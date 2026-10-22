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
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException

from ..database import get_db
from ..research_context import (
    _failed_tool_payload,
    _safe_url,
    build_research_context,
    content_text,
    source_timestamp,
)
from ..source_urls import redact_source_text, source_display_payload

TERMINAL = {"completed", "partial", "failed", "cancelled"}
STAGES = {"queued", "discovering", "collecting", "extracting", "checking", "synthesizing"}
SCHEMA_VERSION = 1
MAX_CLAIMS = 60
MAX_DOCUMENTS = 100
BLOCKS = (
    ("boundary", "研究边界"),
    ("conclusions", "核心判断"),
    ("calculations", "数字与计算"),
    ("industry", "产业传导机制"),
    ("counterevidence", "反方与失效条件"),
    ("gaps", "未解决缺口"),
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
                    "publication_basis": "tool_metadata" if published_at else "unknown",
                    "data_period": None,
                    "content_sha256": content_hash,
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


def _records(db, table, run_id):
    # Table names are internal literals, never caller supplied.
    return [
        json.loads(row[0])
        for row in db.execute(f"SELECT payload FROM {table} WHERE run_id=? ORDER BY rowid", [run_id])
    ]


def read_research_run(username: str, run_id: str) -> dict:
    init_research_db()
    with get_db() as db:
        run = _owned(db, username, run_id)
        return source_display_payload(
            {
                "run": run,
                "documents": _records(db, "research_documents", run_id),
                "passages": _records(db, "research_passages", run_id),
                "claims": _records(db, "research_claims", run_id),
                "report_revisions": _records(db, "research_revisions", run_id),
                "relations": _records(db, "research_relations", run_id),
                "gaps": run["gaps"],
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
        result = Decimal(str(value).replace(",", ""))
        return result if result.is_finite() and abs(result) < Decimal("1e100") else None
    except (InvalidOperation, ValueError):
        return None


def _claim(candidate, passages, documents, existing):
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
        pid not in all_ids or not isinstance(quote, str) for pid, quote in quotes.items()
    ):
        raise HTTPException(422, "引文必须绑定本轮引用")
    if any(not quote.strip() or quote not in passages[pid]["excerpt"] for pid, quote in quotes.items()):
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
    value = _short(str(candidate["value"]), 100) if candidate.get("value") is not None else None
    formula = None
    if value is not None and (not period or not unit):
        gaps.append("numeric_basis_missing")
    if kind == "fact" and value is not None:
        numeric = _decimal(value)
        numbers = [
            _decimal(n) for p in supports for n in re.findall(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?", p["excerpt"])
        ]
        if numeric is None or numeric not in numbers:
            gaps.append("value_not_in_excerpt")
        if unit and not any(unit in p["excerpt"] for p in supports):
            gaps.append("unit_not_in_excerpt")
        if period and not any(period in p["excerpt"] for p in supports):
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
            if None in values or any(c["citation_status"] != "valid" for c in (a, b)):
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
                    computed = operations[raw["operator"]]()
                    formula["computed_value"] = str(computed)
                    if _decimal(value) != computed:
                        gaps.append("calculation_mismatch")
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
        "entities": [_short(v, 120) for v in candidate.get("entities", [])[:20] if isinstance(v, str)]
        if isinstance(candidate.get("entities", []), list)
        else [],
        "formula": formula,
        "limitations": [v[:500] for v in limitations[:20]],
        "citation_status": "invalid" if "quote_not_in_passage" in gaps else ("partial" if gaps else "valid"),
        "semantic_status": "unreviewed",
        "gaps": gaps,
        "created_at": _now(),
    }


def record_claims(username: str, run_id: str, claims: list[dict]) -> dict:
    if not isinstance(claims, list) or not 1 <= len(claims) <= 20:
        raise HTTPException(422, "每次登记 1–20 条候选结论")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id, writable=True)
            passages = {p["passage_id"]: p for p in _records(db, "research_passages", run_id)}
            documents = {d["document_id"]: d for d in _records(db, "research_documents", run_id)}
            existing = {c["claim_id"]: c for c in _records(db, "research_claims", run_id)}
            if len(existing) + len(claims) > MAX_CLAIMS:
                raise HTTPException(422, "本轮结论数量已达上限")
            result = []
            for candidate in claims:
                claim = _claim(candidate, passages, documents, existing)
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
    return {"claims": result, "semantic_status": "unreviewed"}


def finalize_research_run(username: str, run_id: str, text: str, *, error=False, cancelled=False) -> dict:
    """Freeze one report per run. Retrying returns the identical saved revision."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            run = _owned(db, username, run_id)
            old = db.execute("SELECT payload FROM research_revisions WHERE run_id=?", [run_id]).fetchone()
            if old:
                db.execute("COMMIT")
                return json.loads(old[0])
            claims = _records(db, "research_claims", run_id)
            relations = _records(db, "research_relations", run_id)
            gaps = list(run["gaps"])
            if not claims:
                gaps.append("structured_claims_missing")
            if any(c["citation_status"] != "valid" for c in claims):
                gaps.append("citation_checks_incomplete")
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
            if claims:
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
                    block_text = f"{run['question']}\n观察时点：{run['as_of']}（{run['timezone']}）"
                    if run.get("information_cutoff"):
                        precision_label = "日精度" if run["cutoff_precision"] == "day" else "秒精度"
                        block_text += f"\n信息截至：{run['information_cutoff']}（{precision_label}）"
                    if run.get("requested_period"):
                        block_text += f"\n研究期间：{run['requested_period']}"
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
            status = (
                "cancelled"
                if cancelled
                else ("failed" if error and not text.strip() else "partial" if gaps else "completed")
            )
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
                "narrative": text[:200000],
                "narrative_status": "model_output_unreviewed",
                "acceptance_status": status,
                "semantic_status": "unreviewed",
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
