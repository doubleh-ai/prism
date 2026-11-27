"""A receipt ledger, not an automatic truth oracle.

Only the server's known successful tool calls may create source documents. Short
excerpts retain their actual scope: search snippets and tool-transformed text are
not originals. Claims are model candidates; structural checks never certify their
meaning. No network, filesystem, model or arbitrary expression execution here.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import suppress
from datetime import date
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException

from ..auth import require_active_account
from ..database import get_db
from ..research_context import (
    _failed_tool_payload,
    _safe_url,
    build_research_context,
    content_text,
    source_timestamp,
)
from ..source_urls import redact_source_text, source_display_payload
from .evidence_gate import claim_involves_financial_number, original_evidence_missing
from .locator import auto_locate_claims
from .quality_bar import ADVISORY_GAPS, classify_scenario, is_below_bar, quality_gate
from .quantities import audit_claims, period_matches
from .reconciliation import (
    MIN_COVERAGE,
    claim_measure_key,
    normalized_claim_key,
    reconcile_report,
    report_display_measurement,
    report_number_claims,
    same_measurement,
    unavailable_result,
)
from .render import render_report, unavailable_view

TERMINAL = {"completed", "partial", "failed", "cancelled", "blocked"}
STAGES = {"queued", "discovering", "collecting", "extracting", "checking", "synthesizing"}
SCHEMA_VERSION = 1
MAX_CLAIMS = 60
# Bound the upgrade round's claim list: the model is asked to fill source pages
# for the report numbers the server already registered, one compact row each.
MAX_UPGRADE_CANDIDATES = 20
MAX_DOCUMENTS = 100
MAX_ORIGINAL_CHARS = 2_000_000
MAX_ORIGINAL_PAGES = 2000
BLOCKS = (
    ("boundary", "研究边界"),
    ("conclusions", "核心判断"),
    ("calculations", "数字与计算"),
    ("industry", "产业传导机制"),
    ("counterevidence", "反方与失效条件"),
    ("gaps", "未解决缺口"),
)
# 核验状态记录结论表述的核对方式，不自动证明结论为真：
# verified_source 原文已核验 / secondhand 二手转述 / inferred 模型推断 / unverified 未核验。
VERIFICATION_LEVELS = ("verified_source", "secondhand", "inferred", "unverified")
DEFAULT_VERIFICATION = "unverified"


class OriginalEvidenceRequired(HTTPException):
    """A verified_source claim is rejected until this run obtains original text."""

    error_code = "original_required"

    def __init__(self):
        super().__init__(
            422,
            "本次研究尚未取得任何原文文档，标 verified_source 的结论已被拒绝；"
            "请先调用 source_original 取得公告PDF原文并记录 document_id 和命中页码，"
            "或把该结论改标 secondhand 后重新登记。",
        )


def _json(value):
    return json.dumps(
        source_display_payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now():
    return build_research_context()["as_of"]


def _id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def init_research_db():
    """Additive, idempotent schema; never rewrites legacy reports or sources."""
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS research_runs (
                run_id TEXT PRIMARY KEY, username TEXT NOT NULL, conv_id TEXT,
                created_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_research_runs_owner
                ON research_runs(username, created_at DESC);
            CREATE TABLE IF NOT EXISTS research_documents (
                document_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                receipt_key TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(run_id, receipt_key)
            );
            CREATE TABLE IF NOT EXISTS research_passages (
                passage_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                document_id TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_research_passages_run ON research_passages(run_id);
            CREATE TABLE IF NOT EXISTS research_claims (
                claim_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_research_claims_run ON research_claims(run_id);
            CREATE TABLE IF NOT EXISTS research_revisions (
                revision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_relations (
                relation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_research_relations_run ON research_relations(run_id);
        """)


def _owned(db, username, run_id, *, writable=False):
    row = db.execute(
        "SELECT r.payload FROM research_runs r WHERE r.run_id=? AND r.username=? "
        "AND (r.conv_id IS NULL OR EXISTS (SELECT 1 FROM conversations c "
        "WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL))",
        [run_id, username],
    ).fetchone()
    if not row:
        raise HTTPException(404, "研究不存在")
    run = json.loads(row[0])
    if writable and run["status"] in TERMINAL:
        raise HTTPException(409, "该研究版本已固定；请新建一轮研究")
    return run


def _save_run(db, run):
    db.execute("UPDATE research_runs SET payload=? WHERE run_id=?", [_json(run), run["run_id"]])


def create_research_run(username: str, conv_id: str, question: str, context: dict) -> dict:
    init_research_db()
    run_id = context["run_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise HTTPException(422, "无效研究编号")
    with get_db() as db:
        if (
            conv_id
            and not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
                [conv_id, username],
            ).fetchone()
        ):
            raise HTTPException(404, "对话不存在")
        if db.execute("SELECT 1 FROM research_runs WHERE run_id=?", [run_id]).fetchone():
            return _owned(db, username, run_id)
        run = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "conv_id": conv_id or None,
            "question": question[:10000],
            "as_of": context["as_of"],
            "timezone": "Asia/Shanghai",
            "requested_period": None,
            "information_cutoff": None,
            "cutoff_precision": "unknown",
            "cutoff_basis": None,
            "attempt": 1,
            "scenario": None,
            "status": "queued",
            "created_at": _now(),
            "finished_at": None,
            "gaps": [],
        }
        db.execute(
            "INSERT INTO research_runs VALUES (?,?,?,?,?)",
            [run_id, username, conv_id or None, run["created_at"], _json(run)],
        )
    return run


def set_run_status(username: str, run_id: str, status: str, *, gap: str | None = None):
    if status not in STAGES | TERMINAL:
        raise ValueError("Invalid research stage")
    with get_db() as db:
        run = _owned(db, username, run_id)
        if run["status"] in TERMINAL:
            return run
        run["status"] = status
        if gap and gap not in run["gaps"]:
            run["gaps"].append(gap[:300])
        if status in TERMINAL:
            run["finished_at"] = _now()
        _save_run(db, run)
        return run


def append_run_gap(username: str, run_id: str, gap: str) -> dict:
    """Record one advisory gap on a live run without changing its stage.

    The tool loop uses this for degradations that must stay visible in the run
    payload (for example a claim retry skipped to keep the report call) without
    adding a table, column or status transition. Terminal runs stay immutable.
    """
    with get_db() as db:
        run = _owned(db, username, run_id)
        if run["status"] in TERMINAL:
            return run
        if gap not in run["gaps"]:
            run["gaps"].append(gap[:300])
            _save_run(db, run)
        return run


def _publication(value):
    """Only recorded metadata, never infer publication from retrieval or prose."""
    if not isinstance(value, str):
        return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
            return value, "day"
        except ValueError:
            return None, "unknown"
    timestamp = source_timestamp(value)
    return (timestamp, "second") if timestamp else (None, "unknown")


def set_research_scope(
    username: str, run_id: str, information_cutoff: str, requested_period: str | None = None
) -> dict:
    """Freeze explicit information scope before receipts or conclusions exist.

    This records a tool instruction, not an inferred user intent or a change to
    the actual observation clock. Same-value retries do not rewrite the run.
    """
    if not isinstance(information_cutoff, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))?", information_cutoff
    ):
        raise HTTPException(422, "信息截止须为 YYYY-MM-DD 或含时区的 ISO 秒精度时间")
    cutoff, precision = _publication(information_cutoff)
    if cutoff is None:
        raise HTTPException(422, "信息截止日期无效")
    if requested_period is not None and (
        not isinstance(requested_period, str) or len(requested_period) > 120
    ):
        raise HTTPException(422, "研究期间须为不超过 120 字的文字")
    period = requested_period.strip() or None if requested_period is not None else None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id)
            if cutoff[:10] > run["as_of"][:10] or (precision == "second" and cutoff > run["as_of"]):
                raise HTTPException(422, "信息截止不能晚于本轮真实观察时点")
            if run.get("information_cutoff") == cutoff and run.get("requested_period") == period:
                db.execute("COMMIT")
                return {"run": run}
            if run.get("information_cutoff") or run["status"] in TERMINAL:
                raise HTTPException(409, "本轮研究范围已固定；改变范围请新建一轮研究")
            for table in ("research_documents", "research_claims", "research_relations"):
                if db.execute(f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1", [run_id]).fetchone():
                    raise HTTPException(409, "已取得资料或登记结论；改变研究范围请新建一轮研究")
            run.update(
                information_cutoff=cutoff,
                cutoff_precision=precision,
                cutoff_basis="research_scope_tool",
                requested_period=period,
            )
            _save_run(db, run)
            db.execute("COMMIT")
            return {"run": run}
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _cutoff_gaps(run, published_at, precision):
    cutoff = run.get("information_cutoff")
    if not cutoff:
        return []
    if not published_at:
        return ["historical_availability_unknown"]
    # A day is a day, not an invented midnight. A timestamp cutoff cannot
    # establish that a day-only publication was available earlier that day.
    if published_at[:10] > cutoff[:10]:
        return ["published_after_cutoff"]
    if run.get("cutoff_precision") == "second" and published_at[:10] == cutoff[:10]:
        if precision == "day":
            return ["publication_precision_insufficient"]
        if published_at > cutoff:
            return ["published_after_cutoff"]
    return []


def _canonical_url(url):
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith("utm_")]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(query), ""))


def ingest_tool_result(username: str, run_id: str, call: dict, block: dict, refs: list[dict]) -> list[dict]:
    """Internal ingestion after EvidenceTracker matched a call in this exact turn.

    This function is deliberately not a browser/MCP write capability. It checks the
    call/result binding again and accepts only WebSearch/WebFetch source receipts.
    """
    call_id = str(call.get("id") or call.get("tool_use_id") or "")
    tool = call.get("name")
    text = redact_source_text(content_text(block.get("content")))
    if (
        not call_id
        or str(block.get("tool_use_id") or "") != call_id
        or tool not in {"WebFetch", "WebSearch"}
        or block.get("is_error")
        or block.get("status") in {"error", "failed"}
        or not text
        or len(text) > 2_000_000
        or _failed_tool_payload(text)
    ):
        return refs
    enriched = []
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id, writable=True)
            count = db.execute("SELECT COUNT(*) FROM research_documents WHERE run_id=?", [run_id]).fetchone()[
                0
            ]
            for ref in refs[:MAX_DOCUMENTS]:
                url = _safe_url(ref.get("url"))
                if not url or ref.get("run_id") != run_id or ref.get("tool_call_id") != call_id:
                    continue
                receipt_key = _hash(call_id + "\n" + url)
                old = db.execute(
                    "SELECT payload FROM research_documents WHERE run_id=? AND receipt_key=?",
                    [run_id, receipt_key],
                ).fetchone()
                if old:
                    doc = json.loads(old[0])
                    enriched.append(
                        {**ref, "document_id": doc["document_id"], "passage_ids": doc["passage_ids"]}
                    )
                    continue
                if count >= MAX_DOCUMENTS:
                    if "source_limit" not in run["gaps"]:
                        run["gaps"].append("source_limit")
                    break
                # A search snippet belongs only to its own link, never to the
                # entire result page or another result's article.
                excerpt = (text if tool == "WebFetch" else str(ref.get("snippet") or "")).strip()[:600]
                published_at, precision = _publication(ref.get("published_at"))
                document_id = _id("doc")
                passage_id = _id("passage") if excerpt else None
                scope = "tool_returned_text" if tool == "WebFetch" else "search_snippet"
                content_hash = _hash(text if tool == "WebFetch" else str(ref.get("snippet") or ""))
                doc = {
                    "document_id": document_id,
                    "run_id": run_id,
                    "url": url,
                    "canonical_url": _canonical_url(url),
                    "final_url": None,
                    "title": redact_source_text(str(ref.get("title") or url)[:500]),
                    "publisher": None,
                    "observed_host": urlsplit(url).hostname,
                    "document_number": None,
                    "source_kind": "external_web",
                    "access_method": tool,
                    "tool_call_id": call_id,
                    "request": {
                        key: redact_source_text(value[:2000])
                        for key, value in (call.get("input") or {}).items()
                        if key in {"query", "url"}
                        and isinstance(value, str)
                        and (key != "url" or _safe_url(value))
                    }
                    if isinstance(call.get("input"), dict)
                    else {},
                    "retrieved_at": source_timestamp(ref.get("retrieved_at")),
                    "published_at": published_at,
                    "publication_precision": precision,
                    "publication_basis": (
                        ref.get("published_at_basis")
                        if ref.get("published_at_basis") in {"page_metadata", "source_index_metadata"}
                        else "tool_metadata"
                    )
                    if published_at
                    else "unknown",
                    "source_index": {
                        "url": _safe_url(ref.get("index_source_url")),
                        "body_sha256": ref["index_body_sha256"],
                    }
                    if _safe_url(ref.get("index_source_url"))
                    and isinstance(ref.get("index_body_sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", ref["index_body_sha256"])
                    else None,
                    "data_period": None,
                    "content_sha256": content_hash,
                    "retrieved_body_sha256": ref.get("body_sha256")
                    if isinstance(ref.get("body_sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", ref["body_sha256"])
                    else None,
                    "pdf_page_range": ref.get("page_range")
                    if isinstance(ref.get("page_range"), list)
                    and len(ref["page_range"]) <= 20
                    and all(type(p) is int and 1 <= p <= 2000 for p in ref["page_range"])
                    else None,
                    "receipt_sha256": _hash(text),
                    "hash_basis": "normalized_tool_return"
                    if tool == "WebFetch"
                    else "returned_search_excerpt",
                    "content_scope": scope,
                    "original_obtained": False,
                    "independence_status": "not_assessed",
                    "content_group": content_hash if excerpt else None,
                    "revision_of": None,
                    "passage_ids": [passage_id] if passage_id else [],
                    "gaps": ["original_not_verified"] + ([] if published_at else ["publication_unknown"]),
                }
                if published_at and (
                    published_at[:10] > run["as_of"][:10]
                    or (precision == "second" and published_at > run["as_of"])
                ):
                    doc["gaps"].append("published_after_as_of")
                doc["gaps"].extend(_cutoff_gaps(run, published_at, precision))
                if (tool == "WebFetch" and len(text) > len(excerpt)) or ref.get("snippet_truncated"):
                    doc["gaps"].append("excerpt_truncated")
                db.execute(
                    "INSERT INTO research_documents VALUES (?,?,?,?)",
                    [document_id, run_id, receipt_key, _json(doc)],
                )
                if passage_id:
                    passage = {
                        "passage_id": passage_id,
                        "run_id": run_id,
                        "document_id": document_id,
                        "excerpt": excerpt,
                        "excerpt_sha256": _hash(excerpt),
                        "locator": {
                            "kind": "tool_return_offset" if tool == "WebFetch" else "search_snippet_offset",
                            "start": 0,
                            "end": len(excerpt),
                            "page": None,
                            "section": None,
                        },
                        "extraction_status": "tool_excerpt",
                        "content_scope": scope,
                        "truncated": len(text if tool == "WebFetch" else str(ref.get("snippet") or ""))
                        > len(excerpt),
                    }
                    db.execute(
                        "INSERT INTO research_passages VALUES (?,?,?,?)",
                        [passage_id, run_id, document_id, _json(passage)],
                    )
                enriched.append(
                    {
                        **ref,
                        "document_id": document_id,
                        "passage_ids": doc["passage_ids"],
                        "content_scope": scope,
                    }
                )
                count += 1
            run["status"] = "extracting" if enriched else "collecting"
            _save_run(db, run)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return enriched


def _page_meta(passage):
    return {
        "passage_id": passage["passage_id"],
        "page": passage["locator"]["page"],
        "chars": len(passage["excerpt"]),
        "sha256": passage["excerpt_sha256"],
        "truncated": passage["truncated"],
    }


def ingest_original_document(username: str, run_id: str, call: dict, payload: dict) -> dict:
    """Store one server-fetched PDF as the run's original document plus page passages.

    This is the only writer allowed to set ``original_obtained`` true. It
    re-validates the file digest and page numbering instead of trusting the
    caller, and every physical page becomes its own passage so a claim can name
    the page it actually read.
    """
    call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
    announcement = payload.get("announcement") if isinstance(payload, dict) else None
    pages = payload.get("pages") if isinstance(payload, dict) else None
    pdf_sha256 = payload.get("file_sha256") if isinstance(payload, dict) else None
    file_bytes = payload.get("file_bytes") if isinstance(payload, dict) else None
    page_count = payload.get("page_count") if isinstance(payload, dict) else None
    if (
        not call_id
        or not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or not isinstance(announcement, dict)
        or not isinstance(pages, list)
        or not isinstance(pdf_sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", pdf_sha256)
        or not isinstance(file_bytes, int)
        or not 0 < file_bytes <= 8 * 1024 * 1024
        or not isinstance(page_count, int)
        or not 1 <= page_count <= MAX_ORIGINAL_PAGES
        or len(pages) != page_count
    ):
        raise HTTPException(422, "原始文档内容无效")
    url = _safe_url(announcement.get("pdf_url"))
    if url is None:
        raise HTTPException(422, "原始文档地址无效")
    page_texts = []
    for expected, page in enumerate(pages, start=1):
        if (
            not isinstance(page, dict)
            or page.get("page") != expected
            or not isinstance(page.get("text"), str)
        ):
            raise HTTPException(422, "原始文档页码不连续")
        page_texts.append(page["text"])
    if sum(len(page_text) for page_text in page_texts) > MAX_ORIGINAL_CHARS:
        raise HTTPException(422, "原文文本超过上限")
    truncated_pages = {
        number
        for number in payload.get("truncated_pages") or []
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= page_count
    }
    title = redact_source_text(str(announcement.get("title") or url)[:500])
    publisher = redact_source_text(str(announcement.get("publisher") or "巨潮资讯网")[:200])
    document_number = _short(announcement.get("announcement_id"), 80)
    published_at, precision = _publication(announcement.get("published_at"))
    retrieved_at = source_timestamp(payload.get("retrieved_at")) or _now()
    receipt_key = _hash(call_id + "\n" + url)
    request = {"query": redact_source_text(str(payload.get("query") or "")[:2000])}
    index = payload.get("index")
    source_index = None
    if isinstance(index, dict):
        index_url = _safe_url(index.get("url"))
        index_sha = index.get("body_sha256")
        if index_url and isinstance(index_sha, str) and re.fullmatch(r"[a-f0-9]{64}", index_sha):
            source_index = {"url": index_url, "body_sha256": index_sha}
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id, writable=True)
            old = db.execute(
                "SELECT payload FROM research_documents WHERE run_id=? AND receipt_key=?",
                [run_id, receipt_key],
            ).fetchone()
            if old:
                document = json.loads(old[0])
                stored = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT payload FROM research_passages WHERE run_id=? AND document_id=? ORDER BY rowid",
                        [run_id, document["document_id"]],
                    )
                ]
                db.execute("COMMIT")
                return {"document": document, "passages": [_page_meta(passage) for passage in stored]}
            count = db.execute("SELECT COUNT(*) FROM research_documents WHERE run_id=?", [run_id]).fetchone()[
                0
            ]
            if count >= MAX_DOCUMENTS:
                raise HTTPException(422, "本轮原文文档数量已达上限")
            document_id = _id("doc")
            stored_texts = [redact_source_text(page_text) for page_text in page_texts]
            content_sha256 = _hash("\n".join(stored_texts))
            passages = []
            passage_ids = []
            for number, page_text in enumerate(stored_texts, start=1):
                passage_id = _id("passage")
                passage_ids.append(passage_id)
                passages.append(
                    {
                        "passage_id": passage_id,
                        "run_id": run_id,
                        "document_id": document_id,
                        "excerpt": page_text,
                        "excerpt_sha256": _hash(page_text),
                        "page_sha256": _hash(page_text),
                        "locator": {
                            "kind": "pdf_page",
                            "start": 0,
                            "end": len(page_text),
                            "page": number,
                            "section": None,
                        },
                        "extraction_status": "original_pdf_page",
                        "content_scope": "original_pdf_page",
                        "truncated": number in truncated_pages,
                    }
                )
            doc = {
                "document_id": document_id,
                "run_id": run_id,
                "url": url,
                "canonical_url": _canonical_url(url),
                "final_url": _safe_url(payload.get("pdf_url")),
                "title": title,
                "publisher": publisher,
                "observed_host": urlsplit(url).hostname,
                "document_number": document_number,
                "source_kind": "announcement_pdf",
                "access_method": "source_original",
                "tool_call_id": call_id,
                "request": request,
                "retrieved_at": retrieved_at,
                "published_at": published_at,
                "publication_precision": precision,
                "publication_basis": "announcement_index_metadata" if published_at else "unknown",
                "source_index": source_index,
                "data_period": None,
                "content_sha256": content_sha256,
                "retrieved_body_sha256": pdf_sha256,
                "file_sha256": pdf_sha256,
                "file_bytes": file_bytes,
                "page_count": page_count,
                "pdf_page_range": None,
                "receipt_sha256": pdf_sha256,
                "hash_basis": "original_pdf_file",
                "content_scope": "original_pdf",
                "original_obtained": True,
                "independence_status": "not_assessed",
                "content_group": pdf_sha256,
                "revision_of": None,
                "passage_ids": passage_ids,
                "gaps": [] if published_at else ["publication_unknown"],
            }
            if published_at and (
                published_at[:10] > run["as_of"][:10]
                or (precision == "second" and published_at > run["as_of"])
            ):
                doc["gaps"].append("published_after_as_of")
            doc["gaps"].extend(_cutoff_gaps(run, published_at, precision))
            if truncated_pages:
                doc["gaps"].append("excerpt_truncated")
            db.execute(
                "INSERT INTO research_documents VALUES (?,?,?,?)",
                [document_id, run_id, receipt_key, _json(doc)],
            )
            for passage in passages:
                db.execute(
                    "INSERT INTO research_passages VALUES (?,?,?,?)",
                    [passage["passage_id"], run_id, document_id, _json(passage)],
                )
            run["status"] = "extracting"
            _save_run(db, run)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"document": doc, "passages": [_page_meta(passage) for passage in passages]}


def ingest_market_snapshot(
    username: str,
    run_id: str,
    call: dict,
    snapshot: dict,
    *,
    access_method: str = "futures_snapshot",
    request: dict | None = None,
) -> dict:
    """Store one rendered market-data snapshot as an original, page-addressable document.

    ``futures.snapshot_document``, ``futures_spread.spread_document`` and
    ``futures_inventory.snapshot_document`` render a platform observation into
    source- and date-labelled pages. This writer records that rendering as the
    run's original document, so the same page matcher that handles PDFs can
    associate a report figure with the page that carries it. The caller is not
    trusted: the page numbering, text limits and metadata are re-validated here.
    ``access_method``/``request`` let a second market tool keep its own receipt
    and idempotency key without bypassing this writer.
    """
    if not isinstance(access_method, str) or not access_method:
        raise HTTPException(422, "行情快照内容无效")
    call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
    observation = snapshot if isinstance(snapshot, dict) else {}
    symbol = observation.get("symbol")
    title = observation.get("title")
    source = observation.get("source")
    pages = observation.get("pages")
    sections = observation.get("sections")
    data_period, precision = _publication(observation.get("data_period"))
    url = _safe_url(observation.get("url"))
    if (
        not call_id
        or not isinstance(symbol, str)
        or not 1 <= len(symbol) <= 12
        or precision != "day"
        or url is None
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(source, dict)
        or not isinstance(source.get("name"), str)
        or _safe_url(source.get("url")) is None
        or not isinstance(pages, list)
        or not 1 <= len(pages) <= MAX_ORIGINAL_PAGES
        or not isinstance(sections, list)
        or len(sections) != len(pages)
    ):
        raise HTTPException(422, "行情快照内容无效")
    page_texts = []
    section_labels = []
    for expected, (page, section) in enumerate(zip(pages, sections, strict=True), start=1):
        if (
            not isinstance(page, dict)
            or page.get("page") != expected
            or not isinstance(page.get("text"), str)
            or not page["text"].strip()
            or not isinstance(section, dict)
            or section.get("page") != expected
        ):
            raise HTTPException(422, "行情快照页码不连续")
        page_texts.append(page["text"])
        label = section.get("label")
        section_labels.append(label if isinstance(label, str) else None)
    if sum(len(text) for text in page_texts) > MAX_ORIGINAL_CHARS:
        raise HTTPException(422, "行情快照文本超过上限")
    page_count = len(page_texts)
    title = redact_source_text(title.strip()[:500])
    publisher = redact_source_text(str(observation.get("publisher") or source["name"])[:200])
    retrieved_at = source_timestamp(observation.get("retrieved_at")) or _now()
    receipt_key = _hash(access_method + "\n" + symbol + "\n" + data_period)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id, writable=True)
            old = db.execute(
                "SELECT payload FROM research_documents WHERE run_id=? AND receipt_key=?",
                [run_id, receipt_key],
            ).fetchone()
            if old:
                document = json.loads(old[0])
                stored = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT payload FROM research_passages WHERE run_id=? AND document_id=? ORDER BY rowid",
                        [run_id, document["document_id"]],
                    )
                ]
                db.execute("COMMIT")
                return {"document": document, "passages": [_page_meta(passage) for passage in stored]}
            count = db.execute("SELECT COUNT(*) FROM research_documents WHERE run_id=?", [run_id]).fetchone()[
                0
            ]
            if count >= MAX_DOCUMENTS:
                raise HTTPException(422, "本轮原文文档数量已达上限")
            document_id = _id("doc")
            stored_texts = [redact_source_text(text) for text in page_texts]
            content_sha256 = _hash("\n".join(stored_texts))
            snapshot_sha256 = _hash(
                _json({key: value for key, value in observation.items() if key != "pages"})
            )
            passages = []
            passage_ids = []
            for number, page_text in enumerate(stored_texts, start=1):
                passage_id = _id("passage")
                passage_ids.append(passage_id)
                passages.append(
                    {
                        "passage_id": passage_id,
                        "run_id": run_id,
                        "document_id": document_id,
                        "excerpt": page_text,
                        "excerpt_sha256": _hash(page_text),
                        "page_sha256": _hash(page_text),
                        "locator": {
                            "kind": "data_page",
                            "start": 0,
                            "end": len(page_text),
                            "page": number,
                            "section": section_labels[number - 1],
                        },
                        "extraction_status": "market_data_snapshot_page",
                        "content_scope": "market_data_snapshot_page",
                        "truncated": False,
                    }
                )
            doc = {
                "document_id": document_id,
                "run_id": run_id,
                "url": url,
                "canonical_url": _canonical_url(url),
                "final_url": None,
                "title": title,
                "publisher": publisher,
                "observed_host": urlsplit(url).hostname,
                "document_number": f"{symbol}-{data_period}",
                "source_kind": "market_data_snapshot",
                "access_method": access_method,
                "tool_call_id": call_id,
                "request": request if isinstance(request, dict) else {"symbol": symbol},
                "retrieved_at": retrieved_at,
                "published_at": data_period,
                "publication_precision": precision,
                "publication_basis": "data_date",
                "source_index": None,
                "data_period": data_period,
                "content_sha256": content_sha256,
                "retrieved_body_sha256": snapshot_sha256,
                "file_sha256": content_sha256,
                "file_bytes": None,
                "page_count": page_count,
                "pdf_page_range": [1, page_count],
                "receipt_sha256": content_sha256,
                "hash_basis": "market_data_snapshot_content",
                "content_scope": "market_data_snapshot",
                "original_obtained": True,
                "independence_status": "not_assessed",
                "content_group": content_sha256,
                "revision_of": None,
                "symbol": symbol,
                "name": observation.get("name"),
                "exchange": observation.get("exchange"),
                "tradable_contract": observation.get("tradable_contract") is True,
                "source": {
                    "name": source["name"],
                    "url": _safe_url(source["url"]),
                    "kind": source.get("kind"),
                },
                "sections": [
                    {
                        "page": section.get("page"),
                        "section": section.get("section"),
                        "label": section_labels[number - 1],
                        "source": section.get("source"),
                        "url": _safe_url(section.get("url")),
                        "data_period": _publication(section.get("data_period"))[0],
                        "symbol": observation.get("symbol"),
                        "name": observation.get("name"),
                    }
                    for number, section in enumerate(sections, start=1)
                ],
                "snapshot_sha256": snapshot_sha256,
                "passage_ids": passage_ids,
                "gaps": [],
            }
            if data_period > run["as_of"][:10]:
                doc["gaps"].append("published_after_as_of")
            doc["gaps"].extend(_cutoff_gaps(run, data_period, precision))
            db.execute(
                "INSERT INTO research_documents VALUES (?,?,?,?)",
                [document_id, run_id, receipt_key, _json(doc)],
            )
            for passage in passages:
                db.execute(
                    "INSERT INTO research_passages VALUES (?,?,?,?)",
                    [passage["passage_id"], run_id, document_id, _json(passage)],
                )
            run["status"] = "extracting"
            _save_run(db, run)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"document": doc, "passages": [_page_meta(passage) for passage in passages]}


def _records(db, table, run_id):
    # Table names are internal literals, never caller supplied.
    return [
        json.loads(row[0])
        for row in db.execute(f"SELECT payload FROM {table} WHERE run_id=? ORDER BY rowid", [run_id])
    ]


def read_research_run(username: str, run_id: str) -> dict:
    init_research_db()
    from .history_checks import cross_run_checks
    from .knowledge import change_notices, init_knowledge_db
    from .model_review import present as present_model_review

    init_knowledge_db()
    with get_db() as db:
        run = _owned(db, username, run_id)
        claims = _records(db, "research_claims", run_id)
        return source_display_payload(
            {
                "run": run,
                "documents": _records(db, "research_documents", run_id),
                "passages": _records(db, "research_passages", run_id),
                "claims": claims,
                "numerical_checks": audit_claims(claims),
                "history_checks": cross_run_checks(db, username, run_id),
                "model_review": present_model_review(run, claims),
                "report_revisions": _records(db, "research_revisions", run_id),
                "relations": _records(db, "research_relations", run_id),
                "gaps": run["gaps"],
                "knowledge_changes": change_notices(db, username, run_id),
            }
        )


def list_research_runs(username: str) -> list[dict]:
    init_research_db()
    with get_db() as db:
        rows = db.execute(
            "SELECT r.payload FROM research_runs r WHERE r.username=? "
            "AND (r.conv_id IS NULL OR EXISTS (SELECT 1 FROM conversations c "
            "WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL)) "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 100",
            [username],
        ).fetchall()
        return source_display_payload([json.loads(row[0]) for row in rows])


def _short(value, limit=500):
    return value[:limit] if isinstance(value, str) else None


def _decimal(value):
    try:
        raw = str(value)
        if len(raw) > 100 or not re.fullmatch(
            r"[+-]?(?:\d{1,3}(?:,\d{3}){1,25}|\d{1,80})(?:\.\d{1,24})?(?:[eE][+-]?\d{1,3})?",
            raw,
            flags=re.ASCII,
        ):
            return None
        result = Decimal(raw.replace(",", ""))
        return result if result.is_finite() and result.copy_abs() < Decimal("1e100") else None
    except (InvalidOperation, ValueError):
        return None


def _original_page_gap(locator, value, passages, documents):
    """Return (code, detail) when a declared verified_source lacks original page support."""

    if not isinstance(locator, dict):
        return "source_locator_missing", "verified_source 结论必须提供 source_locator{document_id,page}"
    document_id = locator.get("document_id")
    if not isinstance(document_id, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", document_id):
        return "locator_invalid", "source_locator.document_id 无效"
    document = documents.get(document_id)
    if document is None:
        return "document_not_found", f"document_id {document_id} 不属于本轮研究"
    if document.get("original_obtained") is not True:
        return "document_not_original", f"文档 {document_id} 不是原文取得"
    page_count = document.get("page_count")
    if not isinstance(page_count, int) or not 1 <= page_count <= MAX_ORIGINAL_PAGES:
        return "document_not_original", f"文档 {document_id} 不是按页原文"
    page = locator.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 2000:
        return "page_invalid", "source_locator.page 须为1–2000的整数"
    if page > page_count:
        return "page_out_of_range", f"第 {page} 页超出文档 {document_id} 的 {page_count} 页"
    excerpt = None
    for passage in passages.values():
        if passage.get("document_id") != document_id:
            continue
        passage_locator = passage.get("locator")
        if (
            isinstance(passage_locator, dict)
            and passage_locator.get("page") == page
            and isinstance(passage.get("excerpt"), str)
        ):
            excerpt = passage["excerpt"]
            break
    if excerpt is None:
        return "page_text_unavailable", f"文档 {document_id} 第 {page} 页没有登记文本"
    if value is None:
        return "claim_value_missing", "verified_source 结论须填写可与原文核对的数值"
    numeric = _decimal(value)
    numbers = [
        _decimal(number)
        for number in re.findall(r"(?<![A-Za-z0-9_.])-?\d[\d,]*(?:\.\d+)?", excerpt, flags=re.ASCII)
    ]
    if numeric is None or numeric not in numbers:
        return "value_not_in_page_text", f"第 {page} 页文本不含结论数值 {value}"
    return None


_QUOTE_WHITESPACE = re.compile(r"\s+")


def _quote_in_excerpt(quote, excerpt):
    """Match a quote across extraction whitespace and PDF line wraps.

    Production run e671f546 quoted a correct sentence but the server-fetched PDF
    text wrapped 归属于 as 归\n属于, so an exact substring check demoted the claim
    to invalid. Collapsing whitespace on both sides keeps the check literal while
    tolerating the layout of extracted source text.
    """
    return _QUOTE_WHITESPACE.sub("", quote) in _QUOTE_WHITESPACE.sub("", excerpt)


# Claim-level gaps a bounded second claim pass can repair. The gaps stay stored
# on the claim; these four are the ones a model fixes by re-registering the same
# measurement with a verbatim quote, the printed value/unit, or the missing
# period/unit basis, so the runtime may spend one more call on them.
_RETRYABLE_CLAIM_GAPS = frozenset(
    {"quote_not_in_passage", "value_not_in_excerpt", "unit_not_in_excerpt", "numeric_basis_missing"}
)
# Content-free field/rule hints for feeding one claim gap back to the model. The
# tool call already returns the claim the model wrote; these hints only name the
# field to correct and the rule that failed, never the claim text or the
# server-fetched excerpt.
_CLAIM_GAP_HINTS = {
    "quote_not_in_passage": ("quotes", "quote not found in bound passage"),
    "support_missing": ("supports", "claim needs at least one supporting passage"),
    "search_snippet_only": (
        "supports",
        "supporting excerpts are search snippets; open the passage text first",
    ),
    "future_publication": ("published_at", "source published after the research as-of time"),
    "published_after_cutoff": ("published_at", "source published after the information cutoff"),
    "historical_availability_unknown": (
        "published_at",
        "source availability at the as-of time is unconfirmable",
    ),
    "publication_precision_insufficient": ("published_at", "publication time precision is insufficient"),
    "numeric_basis_missing": ("period/unit", "value needs both a period and a unit"),
    "value_not_in_excerpt": ("value", "value absent from excerpt"),
    "unit_not_in_excerpt": ("unit", "unit absent from excerpt"),
    "period_not_in_excerpt": ("period", "period absent from excerpt"),
    "formula_missing": ("formula", "calculation claims need a formula"),
    "duplicate_calculation_input": ("formula.input_claim_ids", "formula inputs must differ"),
    "calculation_input_unchecked": (
        "formula.input_claim_ids",
        "formula inputs must pass their own citation checks",
    ),
    "division_by_zero": ("formula", "formula divides by zero"),
    "calculation_out_of_range": ("formula", "formula result is out of the representable range"),
    "calculation_mismatch": ("formula.computed_value", "registered value differs from the computed value"),
    "period_mismatch": ("period", "period differs from the formula inputs"),
    "currency_mismatch": ("currency", "currency differs from the formula inputs"),
    "unit_mismatch": ("unit", "unit differs from the formula inputs"),
    "derived_unit_requires_review": ("unit", "multiply/divide result unit needs manual review"),
    "invalidation_missing": ("limitations", "inference/hypothesis claims need invalidation conditions"),
    "verification_not_supported": (
        "source_locator",
        "declared original page could not be located; open the page or mark secondhand",
    ),
}
_EMPTY_QUOTE_GAP = "quote_empty_ignored"
_EMPTY_QUOTE_REASON = "quote 为空，已按未提供该条引文处理"


def _claim_gap_details(index, gaps, *, period, unit, value, quote_failures):
    """Return one content-free correction hint per claim gap, in stored order.

    ``reason`` names the failed rule and the field only: never the claim text or
    the server-fetched excerpt, so feeding it back cannot re-inflate context.
    """
    details = []
    for gap in dict.fromkeys(gaps):
        if gap == "quote_not_in_passage" and quote_failures:
            details.extend(
                {
                    "claim_index": index,
                    "field": f"quotes.{pid}",
                    "gap": gap,
                    "reason": f"quote not found in passage {pid}",
                }
                for pid in quote_failures
            )
            continue
        field, reason = _CLAIM_GAP_HINTS.get(gap, ("gaps", "citation check failed"))
        if gap == "numeric_basis_missing":
            missing = [name for name, present in (("period", period), ("unit", unit)) if not present]
            if missing:
                field = "/".join(missing)
                reason = f"value {value} needs both a period and a unit"
        elif gap == "value_not_in_excerpt":
            reason = f"value {value} absent from excerpt"
        elif gap == "unit_not_in_excerpt":
            reason = f"unit {unit} absent from excerpt"
        elif gap == "period_not_in_excerpt":
            reason = f"period {period} absent from excerpt"
        details.append({"claim_index": index, "field": field, "gap": gap, "reason": reason})
    return details


def _claim(candidate, passages, documents, existing, *, index):
    if not isinstance(candidate, dict) or candidate.get("kind") not in {
        "fact",
        "calculation",
        "inference",
        "hypothesis",
    }:
        raise HTTPException(422, "结论须区分 fact/calculation/inference/hypothesis")
    statement = _short(candidate.get("statement"), 3000)
    if not statement or not statement.strip():
        raise HTTPException(422, "结论表述不能为空")
    verification = candidate.get("verification", DEFAULT_VERIFICATION)
    if verification is None:
        verification = DEFAULT_VERIFICATION
    if verification not in VERIFICATION_LEVELS:
        raise HTTPException(422, "结论核验状态须为 verified_source/secondhand/inferred/unverified")
    claim_key = candidate.get("claim_key")
    if claim_key is not None and normalized_claim_key(claim_key) is None:
        raise HTTPException(422, "claim_key 须为 research_read 返回的 [维度, 币种, 数值]")
    gaps = []
    links = {}
    for key in ("supports", "opposes", "background"):
        ids = candidate.get(key, [])
        if (
            not isinstance(ids, list)
            or len(ids) > 20
            or any(not isinstance(p, str) or p not in passages for p in ids)
        ):
            raise HTTPException(422, "引用必须属于本轮研究，且不超过 20 条")
        links[key] = list(dict.fromkeys(ids))
    supports = [passages[pid] for pid in links["supports"]]
    all_ids = set(links["supports"] + links["opposes"] + links["background"])
    quotes = candidate.get("quotes", {})
    if not isinstance(quotes, dict) or any(
        pid not in all_ids or not isinstance(quote, str) or len(quote) > 600 for pid, quote in quotes.items()
    ):
        raise HTTPException(422, "引文必须绑定本轮引用，且每条不超过600字")
    # An empty quote means the model supplied no quote for that passage, not a
    # malformed citation: drop the entry instead of failing the whole batch, and
    # report the drop in the claim details so the next turn can add the verbatim
    # text. Non-string quote values stay rejected above.
    empty_quotes = [pid for pid, quote in quotes.items() if not quote.strip()]
    if empty_quotes:
        quotes = {pid: quote for pid, quote in quotes.items() if pid not in empty_quotes}
    if any(not _quote_in_excerpt(quote, passages[pid]["excerpt"]) for pid, quote in quotes.items()):
        gaps.append("quote_not_in_passage")
    kind = candidate["kind"]
    if not supports:
        gaps.append("support_missing")
    if supports and all(p["content_scope"] == "search_snippet" for p in supports):
        gaps.append("search_snippet_only")
    cited_gaps = {gap for pid in all_ids for gap in documents[passages[pid]["document_id"]]["gaps"]}
    if "published_after_as_of" in cited_gaps or "published_after_cutoff" in cited_gaps:
        gaps.append("future_publication")
    gaps.extend(
        sorted(
            cited_gaps
            & {
                "published_after_cutoff",
                "historical_availability_unknown",
                "publication_precision_insufficient",
            }
        )
    )
    period = _short(candidate.get("period"), 120)
    unit = _short(candidate.get("unit"), 80)
    currency = _short(candidate.get("currency"), 30)
    value = str(candidate["value"]) if candidate.get("value") is not None else None
    if value is not None and len(value) > 100:
        raise HTTPException(422, "数字过长，不能截断为另一个数值")
    formula = None
    if value is not None and (not period or not unit):
        gaps.append("numeric_basis_missing")
    # Without a supporting excerpt there is nothing to compare the value, unit or
    # period against: support_missing already says the evidence is absent, and a
    # worthless value_not_in_excerpt would otherwise be reported (and retried)
    # for every unsupported claim.
    if kind == "fact" and value is not None and supports:
        numeric = _decimal(value)
        numbers = [
            _decimal(n)
            for p in supports
            for n in re.findall(r"(?<![A-Za-z0-9_.])-?\d[\d,]*(?:\.\d+)?", p["excerpt"], flags=re.ASCII)
        ]
        if numeric is None or numeric not in numbers:
            gaps.append("value_not_in_excerpt")
        if unit and not any(unit in p["excerpt"] for p in supports):
            gaps.append("unit_not_in_excerpt")
        if period and not any(period_matches(period, p["excerpt"]) for p in supports):
            gaps.append("period_not_in_excerpt")
    if kind == "calculation":
        raw = candidate.get("formula")
        if isinstance(raw, dict) and raw.get("operator") in {"add", "subtract", "multiply", "divide"}:
            inputs = raw.get("input_claim_ids", [])
            if (
                not isinstance(inputs, list)
                or len(inputs) != 2
                or any(not isinstance(i, str) or i not in existing for i in inputs)
            ):
                raise HTTPException(422, "计算只接受本轮已登记的两个输入结论")
            a, b = [existing[i] for i in inputs]
            values = [_decimal(c.get("value")) for c in (a, b)]
            formula = {"operator": raw["operator"], "input_claim_ids": inputs, "computed_value": None}
            if inputs[0] == inputs[1]:
                gaps.append("duplicate_calculation_input")
            elif None in values or any(c["citation_status"] != "valid" for c in (a, b)):
                gaps.append("calculation_input_unchecked")
            else:
                x, y = values
                if raw["operator"] == "divide" and y == 0:
                    gaps.append("division_by_zero")
                else:
                    operations = {
                        "add": lambda: x + y,
                        "subtract": lambda: x - y,
                        "multiply": lambda: x * y,
                        "divide": lambda: x / y,
                    }
                    try:
                        with localcontext() as context:
                            context.prec = 220
                            context.Emax, context.Emin = 10000, -10000
                            computed = operations[raw["operator"]]()
                            if computed.copy_abs() >= Decimal("1e100") or (
                                computed != 0 and computed.adjusted() < -999
                            ):
                                gaps.append("calculation_out_of_range")
                            else:
                                formula["computed_value"] = str(computed)
                                if _decimal(value) != computed:
                                    gaps.append("calculation_mismatch")
                    except DecimalException:
                        gaps.append("calculation_out_of_range")
            if a.get("period") != b.get("period") or period != a.get("period"):
                gaps.append("period_mismatch")
            if a.get("currency") != b.get("currency") or currency != a.get("currency"):
                gaps.append("currency_mismatch")
            if raw["operator"] in {"add", "subtract"} and (
                a.get("unit") != b.get("unit") or unit != a.get("unit")
            ):
                gaps.append("unit_mismatch")
            if raw["operator"] in {"multiply", "divide"}:
                gaps.append("derived_unit_requires_review")
            if "support_missing" in gaps:
                gaps.remove("support_missing")  # Inputs carry their own citation chains.
        else:
            gaps.append("formula_missing")
    limitations = candidate.get("limitations", [])
    if not isinstance(limitations, list) or any(not isinstance(v, str) for v in limitations):
        raise HTTPException(422, "限制条件须为文字列表")
    if kind in {"inference", "hypothesis"} and not limitations:
        gaps.append("invalidation_missing")
    verification_declared = None
    verification_reason = None
    if verification == "verified_source" and not any(
        document.get("original_obtained") is True for document in documents.values()
    ):
        # Without any original document in this run, a first-hand verification
        # cannot even be checked; fail loudly so the model must open the source
        # first instead of silently settling for secondhand evidence.
        raise OriginalEvidenceRequired()
    if verification == "verified_source":
        unsupported = _original_page_gap(candidate.get("source_locator"), value, passages, documents)
        if unsupported is not None:
            # The model declared an original read the server cannot locate in
            # this run. Keep the claim, but never certify it as first-hand.
            verification_declared = "verified_source"
            verification = "secondhand"
            gaps.append("verification_not_supported")
            verification_reason = {"code": unsupported[0], "detail": unsupported[1]}
    source_locator = candidate.get("source_locator")
    if not isinstance(source_locator, dict):
        source_locator = None
    quote_failures = [
        pid for pid, quote in quotes.items() if not _quote_in_excerpt(quote, passages[pid]["excerpt"])
    ]
    details = _claim_gap_details(
        index, gaps, period=period, unit=unit, value=value, quote_failures=quote_failures
    )
    details.extend(
        {
            "claim_index": index,
            "field": f"quotes.{pid}",
            "gap": _EMPTY_QUOTE_GAP,
            "reason": _EMPTY_QUOTE_REASON,
        }
        for pid in empty_quotes
    )
    return {
        "claim_id": _id("claim"),
        "kind": kind,
        "statement": statement,
        **links,
        "quotes": {k: v[:600] for k, v in quotes.items()},
        "period": period,
        "value": value,
        "unit": unit,
        "currency": currency,
        "accounting_basis": _short(candidate.get("accounting_basis"), 300),
        "metric": _short(candidate.get("metric"), 80),
        "source": _short(candidate.get("source"), 500),
        "published_at": _short(candidate.get("published_at"), 40),
        "scope": _short(candidate.get("scope"), 300),
        "entities": [_short(v, 120) for v in candidate.get("entities", [])[:20] if isinstance(v, str)]
        if isinstance(candidate.get("entities", []), list)
        else [],
        "formula": formula,
        "limitations": [v[:500] for v in limitations[:20]],
        "citation_status": "invalid" if "quote_not_in_passage" in gaps else ("partial" if gaps else "valid"),
        "semantic_status": "unreviewed",
        "verification": verification,
        "verification_declared": verification_declared,
        "verification_reason": verification_reason,
        "source_locator": source_locator,
        "gaps": gaps,
        "created_at": _now(),
    }, details


def _matched_claim(existing: dict, candidate: dict) -> dict | None:
    """Find the already registered claim describing the candidate's measurement."""
    return next((claim for claim in existing.values() if same_measurement(claim, candidate)), None)


def _claim_key_match(existing: dict, candidate: dict) -> dict | None:
    """Find a server-registered claim by the model-supplied ``claim_key``.

    The upgrade pass echoes the key from ``research_read`` so a number keeps its
    original claim identity even when the registration omits period/unit details
    that would otherwise change the derived key.
    """
    key = normalized_claim_key(candidate.get("claim_key"))
    if key is None:
        return None
    return next((claim for claim in existing.values() if claim_measure_key(claim) == key), None)


_UPGRADE_KEEP_EMPTY = (
    "source",
    "published_at",
    "scope",
    "source_locator",
    "supports",
    "quotes",
    "entities",
    "formula",
    "period",
)


def _verification_rank(claim: dict) -> int:
    label = claim.get("verification")
    return VERIFICATION_LEVELS.index(label) if label in VERIFICATION_LEVELS else len(VERIFICATION_LEVELS)


def _upgrade_claim(existing: dict, candidate: dict) -> dict:
    """Fold a newly validated model claim into an existing measurement in place.

    The claim id, creation time and server-extracted measurement key stay. The
    model-authored fields replace their current values, except that empty
    provenance fields never erase what is already recorded and the verification
    level only ever upgrades: re-registering a number cannot downgrade a claim
    the model already verified.
    """
    upgraded = dict(existing)
    for field, value in candidate.items():
        if field in {"claim_id", "created_at", "run_id", "claim_key", "origin"}:
            continue
        if field in {"verification", "verification_declared", "verification_reason"}:
            continue
        if field in _UPGRADE_KEEP_EMPTY and value in (None, "", [], {}):
            continue
        upgraded[field] = value
    if _verification_rank(candidate) < _verification_rank(existing):
        upgraded["verification"] = candidate["verification"]
        upgraded["verification_declared"] = candidate.get("verification_declared")
        upgraded["verification_reason"] = candidate.get("verification_reason")
    return upgraded


def _register_report_claims(db, run_id: str, text: str, claims: list[dict]) -> list[dict]:
    """Insert one unverified server claim for every report number left unregistered.

    This is the fixed server-side registration step: reconciliation reads the
    claims table, so the ledger itself must guarantee that every number in the
    report body has a claim, whether or not the model ran its claim pass.
    """
    generated = []
    for candidate in report_number_claims(text, claims):
        claim = {**candidate, "claim_id": _id("claim"), "run_id": run_id, "created_at": _now()}
        db.execute("INSERT INTO research_claims VALUES (?,?,?)", [claim["claim_id"], run_id, _json(claim)])
        generated.append(claim)
    return generated


def record_claims(username: str, run_id: str, claims: list[dict]) -> dict:
    if not isinstance(claims, list) or not 1 <= len(claims) <= 20:
        raise HTTPException(422, "每次登记 1–20 条候选结论")
    from .history_checks import cross_run_checks
    from .knowledge import init_knowledge_db

    init_knowledge_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            require_active_account(db, username)
            run = _owned(db, username, run_id, writable=True)
            passages = {p["passage_id"]: p for p in _records(db, "research_passages", run_id)}
            documents = {d["document_id"]: d for d in _records(db, "research_documents", run_id)}
            existing = {c["claim_id"]: c for c in _records(db, "research_claims", run_id)}
            result = []
            claim_details = []
            for index, candidate in enumerate(claims, start=1):
                claim, details = _claim(candidate, passages, documents, existing, index=index)
                claim_details.extend(details)
                matched = _claim_key_match(existing, candidate) or _matched_claim(existing, claim)
                if matched is not None:
                    # The number is already registered (usually by the server
                    # extraction step). Upgrade that claim instead of inserting a
                    # duplicate: the measurement keeps its original identity while
                    # the model-supplied provenance replaces the empty shell.
                    upgraded = _upgrade_claim(matched, claim)
                    db.execute(
                        "UPDATE research_claims SET payload=? WHERE claim_id=? AND run_id=?",
                        [_json(upgraded), matched["claim_id"], run_id],
                    )
                    existing[matched["claim_id"]] = upgraded
                    result.append(upgraded)
                    continue
                if len(existing) + 1 > MAX_CLAIMS:
                    raise HTTPException(422, "本轮结论数量已达上限")
                claim["run_id"] = run_id
                db.execute(
                    "INSERT INTO research_claims VALUES (?,?,?)", [claim["claim_id"], run_id, _json(claim)]
                )
                result.append(claim)
                existing[claim["claim_id"]] = claim
            run["status"] = "checking"
            _save_run(db, run)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    with get_db() as db:
        return {
            "claims": result,
            # Structured, content-free claim-gap hints so the next claim pass can
            # correct the exact field instead of guessing from a generic error.
            "claim_details": claim_details,
            "retryable": any(detail["gap"] in _RETRYABLE_CLAIM_GAPS for detail in claim_details),
            "semantic_status": "unreviewed",
            "numerical_checks": audit_claims(list(existing.values())),
            "history_checks": cross_run_checks(db, username, run_id),
        }


def register_report_claims(username: str, run_id: str, text: str) -> dict:
    """Register the report body's numbers server-side before the report is frozen.

    Finalizing a run always performs this step, so a run whose model skipped the
    claim pass still reaches reconciliation with one claim per report number. The
    explicit call exists for callers that need the registration earlier; it is
    idempotent and leaves run status untouched.
    """
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(422, "报告正文不能为空")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            require_active_account(db, username)
            _owned(db, username, run_id, writable=True)
            existing = _records(db, "research_claims", run_id)
            generated = _register_report_claims(db, run_id, text, existing)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"claims": generated, "claims_registered": len(existing) + len(generated)}


def upgrade_candidates(username: str, run_id: str, text: str) -> dict:
    """Register the report numbers and list the claims still missing provenance.

    The wrap-up upgrade round gets this bounded list so it only has to fill in
    ``source_locator`` and ``source`` for numbers the server already registered;
    numbers the model upgraded itself are no longer listed. Registration is the
    same idempotent server step finalization performs, so an empty or broken
    report body leaves the ledger untouched instead of raising.
    """
    if not isinstance(text, str) or not text.strip():
        return {"claims": [], "claims_registered": 0}
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            require_active_account(db, username)
            _owned(db, username, run_id, writable=True)
            existing = _records(db, "research_claims", run_id)
            generated = _register_report_claims(db, run_id, text, existing)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    pending = []
    for claim in [*existing, *generated]:
        if claim.get("origin") != "server_extract":
            continue
        if claim.get("verification") not in {"unverified", "secondhand"}:
            continue
        if claim.get("source_locator"):
            continue
        display = report_display_measurement(claim)
        value, unit = display if display else (claim.get("value"), claim.get("unit"))
        pending.append(
            {
                "claim_key": claim.get("claim_key"),
                "claim_id": claim.get("claim_id"),
                "value": value,
                "unit": unit,
                "currency": claim.get("currency"),
                "period": claim.get("period"),
                "statement": claim.get("statement"),
            }
        )
        if len(pending) >= MAX_UPGRADE_CANDIDATES:
            break
    return {"claims": pending, "claims_registered": len(existing) + len(generated)}


def _boundary_text(run: dict) -> str:
    """The run's scope statement, shared by normal and evidence-gated revisions."""
    block_text = f"{run['question']}\n观察时点：{run['as_of']}（{run['timezone']}）"
    if run.get("information_cutoff"):
        precision_label = "日精度" if run["cutoff_precision"] == "day" else "秒精度"
        block_text += f"\n信息截至：{run['information_cutoff']}（{precision_label}）"
    if run.get("requested_period"):
        block_text += f"\n研究期间：{run['requested_period']}"
    return block_text


def _evidence_blocked_revision(
    run: dict,
    claims: list,
    documents: list,
    numerical_checks: dict,
    model_review,
    scenario: dict,
    *,
    error=False,
) -> dict:
    """Freeze an evidence-insufficient result instead of a report body.

    The run keeps its receipts: the already fetched documents stay readable in the
    ledger so the next attempt can reuse them instead of refetching.
    """
    gaps = list(run["gaps"]) + ["original_source_missing"]
    if error:
        gaps.append("engine_failed")
    gaps = list(dict.fromkeys(gaps))
    evidence_gate = {
        "status": "blocked",
        "error_code": "original_required",
        "required": "original_document",
        "reason": "original_source_missing",
        "report_withheld": True,
        "claims_checked": len(claims),
        "financial_claims": sum(1 for claim in claims if claim_involves_financial_number(claim)),
        "documents_retained": len(documents),
        "original_documents": sum(1 for document in documents if document.get("original_obtained") is True),
    }
    blocks = [
        {
            "kind": "boundary",
            "title": "研究边界",
            "text": _boundary_text(run),
            "claim_ids": [],
            "relation_ids": [],
        },
        {
            "kind": "gaps",
            "title": "未解决缺口",
            "text": "；".join(gaps),
            "claim_ids": [],
            "relation_ids": [],
        },
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "revision_id": _id("revision"),
        "run_id": run["run_id"],
        "revision": 1,
        "created_at": _now(),
        "as_of": run["as_of"],
        "timezone": run["timezone"],
        "information_cutoff": run.get("information_cutoff"),
        "cutoff_precision": run.get("cutoff_precision", "unknown"),
        "requested_period": run.get("requested_period"),
        "blocks": blocks,
        "narrative": "",
        "narrative_status": "evidence_gate_blocked",
        "acceptance_status": "blocked",
        "error_code": "original_required",
        "semantic_status": "unreviewed",
        "numerical_checks": numerical_checks,
        "claim_reconciliation": unavailable_result(),
        "scenario": scenario["scenario"],
        "scenario_facts": scenario["facts"],
        # No claim was reconciled, so the scenario bar has nothing to evaluate.
        "quality_gate": quality_gate(scenario["scenario"], None),
        "verification_render": unavailable_view(""),
        "model_review": model_review,
        "gaps": gaps,
        "evidence_gate": evidence_gate,
    }


def _snapshot_sections(documents) -> dict[str, dict[int, dict]]:
    """Page metadata of this run's market-data snapshot documents, by document id."""
    sections: dict[str, dict[int, dict]] = {}
    for document in documents if isinstance(documents, (list, tuple)) else ():
        if not isinstance(document, dict) or document.get("source_kind") != "market_data_snapshot":
            continue
        document_id = document.get("document_id")
        rows = document.get("sections")
        if not isinstance(document_id, str) or not isinstance(rows, list):
            continue
        sections[document_id] = {
            row["page"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("page"), int)
        }
    return sections


def _attach_snapshot_provenance(claim: dict, section: dict) -> None:
    """Fill the render's provenance fields from the snapshot page that matched.

    The number was already verified against that page; this only lets the
    evidence list name the source, data date and section instead of leaving the
    row blank. Model-provided provenance is never overwritten.
    """
    data_period = section.get("data_period")
    if not isinstance(data_period, str):
        return
    source = section.get("source") if isinstance(section.get("source"), str) else "市场数据来源"
    label = section.get("label") if isinstance(section.get("label"), str) else "市场数据"
    name = section.get("name") if isinstance(section.get("name"), str) else ""
    symbol = section.get("symbol") if isinstance(section.get("symbol"), str) else ""
    product = f"{name} {symbol}".strip()
    if not claim.get("source"):
        claim["source"] = f"{source}｜{product}｜{label}｜数据日期 {data_period}"[:500]
    if not claim.get("published_at"):
        claim["published_at"] = f"{data_period}T00:00:00+08:00"
    if not claim.get("scope"):
        claim["scope"] = f"{label} · {product}".strip()[:300]


def _apply_auto_locators(db, claims, documents, passages) -> None:
    """Persist the server-side page associations and their negative attempts.

    The matcher upgrades claims in the in-memory list so reconciliation, the
    render and the frozen revision all read the same verification levels; the
    stored claim rows must agree with that list for the next read.
    """
    sections = _snapshot_sections(documents)
    for claim in auto_locate_claims(claims, documents, passages):
        locator = claim.get("source_locator")
        if isinstance(locator, dict) and isinstance(locator.get("document_id"), str):
            section = sections.get(locator["document_id"], {}).get(locator.get("page"))
            if section is not None:
                _attach_snapshot_provenance(claim, section)
        db.execute(
            "UPDATE research_claims SET payload=? WHERE claim_id=?",
            [_json(claim), claim["claim_id"]],
        )


def finalize_research_run(
    username: str,
    run_id: str,
    text: str,
    *,
    error=False,
    cancelled=False,
    enforce_original_gate: bool = True,
) -> dict:
    """Freeze one report per run. Retrying returns the identical saved revision."""
    from .model_review import present as present_model_review

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id)
            old = db.execute("SELECT payload FROM research_revisions WHERE run_id=?", [run_id]).fetchone()
            if old:
                db.execute("COMMIT")
                return json.loads(old[0])
            claims = _records(db, "research_claims", run_id)
            # Gaps describe the model's own registration work. Server-extracted
            # claims are bookkeeping, so keep the model-claim snapshot for those
            # conditions while all downstream views see the completed list.
            model_claims = list(claims)
            relations = _records(db, "research_relations", run_id)
            documents = _records(db, "research_documents", run_id)
            passages = _records(db, "research_passages", run_id)
            # Scenario is decided from the receipts and the run question alone, so
            # the bar is objective and a historical run replays identically.
            scenario = classify_scenario(run.get("question"), documents)
            run["scenario"] = scenario["scenario"]
            numerical_checks = audit_claims(claims)
            if (run.get("model_review") or {}).get("status") == "running":
                run["model_review"] = {
                    **run["model_review"],
                    "status": "failed",
                    "reason": "interrupted",
                    "usage_complete": False,
                    "interrupted_usage_unknown": True,
                }
            model_review = present_model_review(run, claims)
            # The hard gate classifies the report, not the model's registration
            # work: extract one server claim per unregistered report number before
            # checking, so a run whose model never registered or upgraded them
            # cannot publish a financial report without an original document.
            # A broken extractor degrades coverage telemetry, never the report.
            with suppress(Exception):  # bookkeeping must not cost the report
                claims.extend(_register_report_claims(db, run_id, text, claims))
            # Server-side page association: financial claims whose number appears
            # on an obtained original page are verified here, without another
            # model round. Claims with a model locator are left alone, and a run
            # without any original page falls through to the hard gate below.
            with suppress(Exception):  # bookkeeping must not cost the report
                _apply_auto_locators(db, claims, documents, passages)
            # Hard server-side gate: a report whose claims carry financial or
            # trading numbers is withheld until this run obtained original text.
            # A run that failed without any draft has no report body to withhold.
            if (
                enforce_original_gate
                and not cancelled
                and not (error and not text.strip())
                and original_evidence_missing(claims, documents, text)
            ):
                revision = _evidence_blocked_revision(
                    run,
                    claims,
                    documents,
                    numerical_checks,
                    model_review,
                    scenario,
                    error=error,
                )
                db.execute(
                    "INSERT INTO research_revisions VALUES (?,?,?)",
                    [revision["revision_id"], run_id, _json(revision)],
                )
                run.update(
                    status=revision["acceptance_status"],
                    finished_at=revision["created_at"],
                    gaps=revision["gaps"],
                )
                _save_run(db, run)
                db.execute("COMMIT")
                return revision
            # Report-body claims were registered before the hard gate above, so
            # reconciliation and the render read the completed claim list here.
            try:
                claim_reconciliation = reconcile_report(claims, text)
            except Exception:  # noqa: BLE001 - coverage telemetry must not cost the report
                claim_reconciliation = unavailable_result()
            gate = quality_gate(scenario["scenario"], claim_reconciliation.get("verified_claim_ratio"))
            narrative = text[:200000]
            try:
                verification_render = render_report(
                    claims,
                    narrative,
                    documents=documents,
                    passages=passages,
                )
            except Exception:  # noqa: BLE001 - a derived view must not cost the report
                verification_render = unavailable_view(narrative)
            gaps = list(run["gaps"])
            if model_review:
                if model_review["status"] != "completed":
                    gaps.append("model_review_incomplete")
                else:
                    reviewed_ids = {item["claim_id"] for item in model_review["assessments"]}
                    timed_out = {
                        claim_id
                        for claim_id in model_review.get("timeout_claims") or []
                        if claim_id not in reviewed_ids
                    }
                    unreviewed = [claim for claim in model_claims if claim["claim_id"] not in reviewed_ids]
                    if any(claim["claim_id"] not in timed_out for claim in unreviewed):
                        gaps.append("model_review_partial_scope")
                    if any(claim["claim_id"] in timed_out for claim in unreviewed):
                        # A bounded per-batch review timeout is a claim-level gap;
                        # the completed batches still cover the rest of the run.
                        gaps.append("unverified_review_timeout")
                    if any(a["effective_verdict"] != "supported" for a in model_review["assessments"]):
                        gaps.append("model_review_disagreement")
            if numerical_checks["conflicts"]:
                gaps.append("numeric_evidence_conflict")
            if not model_claims:
                gaps.append("structured_claims_missing")
            if any(c["citation_status"] != "valid" for c in model_claims):
                gaps.append("citation_checks_incomplete")
            coverage = claim_reconciliation.get("coverage")
            if (
                claim_reconciliation.get("status") == "ok"
                and coverage is not None
                and coverage < MIN_COVERAGE
            ):
                gaps.append("claim_coverage_low")
            # Advisory only: a report below its scenario bar is still produced, the
            # gap names the condition so the run is not silently reported as clean.
            if is_below_bar(gate):
                gaps.append("quality_below_scenario_threshold")
            gaps.extend(
                gap
                for claim in claims
                for gap in claim["gaps"]
                if gap
                in {
                    "published_after_cutoff",
                    "historical_availability_unknown",
                    "publication_precision_insufficient",
                }
            )
            # The excerpt review is the semantic check this pipeline can run. A
            # completed or attempted review is not "pending": only a review that
            # never started (missing record or explicit not_performed) leaves the
            # run-level semantic review pending.
            if model_claims and (model_review is None or model_review["status"] == "not_performed"):
                gaps.append("semantic_review_pending")
            if error:
                gaps.append("engine_failed")
            if cancelled:
                gaps.append("interrupted")
            if not text.strip():
                gaps.append("report_missing")
            blocks = []
            for kind, title in BLOCKS:
                selected = [
                    c
                    for c in claims
                    if (
                        (kind == "conclusions" and c["kind"] in {"fact", "hypothesis"})
                        or (kind == "calculations" and c["kind"] == "calculation")
                        or (kind == "industry" and c["kind"] == "inference")
                        or (kind == "counterevidence" and (c["opposes"] or c["limitations"]))
                    )
                ]
                block_text = ""
                if kind == "boundary":
                    block_text = _boundary_text(run)
                elif kind == "gaps":
                    block_text = "；".join(dict.fromkeys(gaps))
                elif kind == "counterevidence":
                    block_text = "\n".join(v for c in selected for v in c["limitations"])
                blocks.append(
                    {
                        "kind": kind,
                        "title": title,
                        "text": block_text,
                        "claim_ids": [c["claim_id"] for c in selected],
                        "relation_ids": [r["relation_id"] for r in relations] if kind == "industry" else [],
                    }
                )
            if cancelled:
                status = "cancelled"
            elif error and not text.strip():
                status = "failed"
            elif any(gap not in ADVISORY_GAPS for gap in gaps):
                status = "partial"
            else:
                status = "completed"
            revision = {
                "schema_version": SCHEMA_VERSION,
                "revision_id": _id("revision"),
                "run_id": run_id,
                "revision": 1,
                "created_at": _now(),
                "as_of": run["as_of"],
                "timezone": run["timezone"],
                "information_cutoff": run.get("information_cutoff"),
                "cutoff_precision": run.get("cutoff_precision", "unknown"),
                "requested_period": run.get("requested_period"),
                "blocks": blocks,
                "narrative": narrative,
                "narrative_status": "model_output_unreviewed",
                "acceptance_status": status,
                "semantic_status": "unreviewed",
                "numerical_checks": numerical_checks,
                "claim_reconciliation": claim_reconciliation,
                "scenario": scenario["scenario"],
                "scenario_facts": scenario["facts"],
                "quality_gate": gate,
                "verification_render": verification_render,
                "model_review": model_review,
                "gaps": list(dict.fromkeys(gaps)),
            }
            db.execute(
                "INSERT INTO research_revisions VALUES (?,?,?)",
                [revision["revision_id"], run_id, _json(revision)],
            )
            run.update(status=status, finished_at=revision["created_at"], gaps=revision["gaps"])
            _save_run(db, run)
            db.execute("COMMIT")
            return revision
        except BaseException:
            db.execute("ROLLBACK")
            raise


def record_relations(username: str, run_id: str, relations: list[dict]) -> dict:
    """Record mechanisms as candidates; link counts never establish supply facts."""
    if not isinstance(relations, list) or not 1 <= len(relations) <= 20:
        raise HTTPException(422, "每次登记 1–20 条关系候选")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id, writable=True)
            claims = {c["claim_id"]: c for c in _records(db, "research_claims", run_id)}
            if len(_records(db, "research_relations", run_id)) + len(relations) > 60:
                raise HTTPException(422, "本轮关系数量已达上限")
            result = []
            for candidate in relations:
                if not isinstance(candidate, dict) or candidate.get("dimension") not in {
                    "supply",
                    "certification",
                    "substitution",
                    "cost",
                    "demand",
                }:
                    raise HTTPException(422, "关系须指定供给/认证/替代/成本/需求维度")
                relation = {}
                for key in (
                    "from_entity",
                    "to_entity",
                    "product",
                    "relation_type",
                    "business_stage",
                    "valid_period",
                    "mechanism",
                ):
                    relation[key] = _short(candidate.get(key), 1200 if key == "mechanism" else 200)
                if not all(relation[k] for k in ("from_entity", "to_entity", "product", "mechanism")):
                    raise HTTPException(422, "关系须包含双方、产品和传导机制")
                for key in ("supporting_claims", "opposing_claims"):
                    ids = candidate.get(key, [])
                    if (
                        not isinstance(ids, list)
                        or len(ids) > 20
                        or any(not isinstance(cid, str) or cid not in claims for cid in ids)
                    ):
                        raise HTTPException(422, "关系只能引用本轮已登记结论")
                    relation[key] = list(dict.fromkeys(ids))
                invalidators = candidate.get("invalidators", [])
                if not isinstance(invalidators, list) or any(not isinstance(v, str) for v in invalidators):
                    raise HTTPException(422, "失效条件须为文字列表")
                gaps = []
                if not relation["supporting_claims"]:
                    gaps.append("support_missing")
                if any(claims[c]["citation_status"] != "valid" for c in relation["supporting_claims"]):
                    gaps.append("citation_checks_incomplete")
                for key in ("business_stage", "valid_period"):
                    if not relation[key]:
                        gaps.append(key + "_unknown")
                if not invalidators:
                    gaps.append("invalidation_missing")
                relation.update(
                    relation_id=_id("relation"),
                    run_id=run_id,
                    dimension=candidate["dimension"],
                    invalidators=[v[:500] for v in invalidators[:20]],
                    status="has_references" if relation["supporting_claims"] else "unverified",
                    semantic_status="unreviewed",
                    gaps=gaps,
                    created_at=_now(),
                )
                db.execute(
                    "INSERT INTO research_relations VALUES (?,?,?)",
                    [relation["relation_id"], run_id, _json(relation)],
                )
                result.append(relation)
            run["status"] = "synthesizing"
            _save_run(db, run)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"relations": result, "semantic_status": "unreviewed"}


def recover_interrupted_runs() -> int:
    """Startup-only reconciliation for an explicitly configured single worker.

    The caller must establish exclusive process ownership before invoking this.
    Never call from schema initialization or a request: other workers may still
    be doing the work. Existing final revisions and all receipts are retained.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT r.run_id,r.username,r.payload FROM research_runs r "
            "WHERE NOT EXISTS(SELECT 1 FROM research_revisions v WHERE v.run_id=r.run_id) "
            "AND (r.conv_id IS NULL OR EXISTS(SELECT 1 FROM conversations c "
            "WHERE c.id=r.conv_id AND c.username=r.username AND c.deleted_at IS NULL))"
        ).fetchall()
    recovered = 0
    for row in rows:
        run = json.loads(row["payload"])
        if run["status"] in TERMINAL:
            continue
        set_run_status(row["username"], row["run_id"], "checking", gap="interrupted_by_restart")
        finalize_research_run(
            row["username"],
            row["run_id"],
            "服务重启后检测到本轮研究未正常结束。已保留原问题、观察时点及取得的材料和候选结论；"
            "未自动重启模型，未恢复的流式正文不能视为已完成报告。请在原会话发起后续研究。",
        )
        recovered += 1
    return recovered
