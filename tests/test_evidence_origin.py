"""Original-disclosure evidence: local fake PDFs, page passages and the claim gate."""

import hashlib
import io
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from server import capabilities as cap
from server.agent import origin_source
from server.agent import tools as T
from server.agent.prompts import system_prompt
from server.capabilities import ClaimsArgs, capability_catalog
from server.database import get_db
from server.research import (
    create_research_run,
    finalize_research_run,
    ingest_original_document,
    model_review,
    read_research_run,
    record_claims,
)
from server.research.ledger import OriginalEvidenceRequired
from server.research_context import EvidenceTracker, build_research_context
from server.work_lifecycle import Manager

TARGET = "170,899,152,276.34"
PDF_URL = "https://static.cninfo.com.cn/finalpage/2025-04-03/1222993920.PDF"
PAGE_TEXTS = [
    "Guizhou Moutai 2024 Annual Report | 1 / 3 | Table of contents",
    "Section 6 Management discussion | 2 / 3 | Revenue grew 15.71 percent year on year.",
    "Consolidated income statement | 3 / 3 | Operating revenue 170,899,152,276.34 CNY,"
    " net profit 86,228,146,421.65 CNY.",
]
TARGET_PAGE = 3
EMPTY_PAGE = 1


def pdf_bytes(page_texts):
    """Build a local PDF whose pages carry the given ASCII texts (no network)."""

    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=400, height=400)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 10 Tf 20 360 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _announcement(**changes):
    value = {
        "announcement_id": "1222993920",
        "title": "贵州茅台2024年年度报告",
        "sec_code": "600519",
        "sec_name": "贵州茅台",
        "publisher": "巨潮资讯网",
        "published_at": "2025-04-03",
        "pdf_url": PDF_URL,
        "pdf_size_kb": 3542,
    }
    value.update(changes)
    return value


def _fetch_payload(page_texts=None, **changes):
    pages = [{"page": index + 1, "text": text} for index, text in enumerate(page_texts or PAGE_TEXTS)]
    raw = pdf_bytes([page["text"] for page in pages])
    payload = {
        "status": "ok",
        "query": "贵州茅台 2024年年度报告",
        "start_date": None,
        "end_date": None,
        "contains": TARGET,
        "announcement": _announcement(),
        "candidates": [],
        "index": {
            "url": "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            "body_sha256": "a" * 64,
        },
        "pdf_url": PDF_URL,
        "requested_pdf_url": PDF_URL,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "file_bytes": len(raw),
        "page_count": len(pages),
        "pages": pages,
        "truncated_pages": [],
        "retrieved_at": "2026-10-20T18:00:00+08:00",
        "matches": origin_source.find_matches(pages, TARGET),
    }
    payload.update(changes)
    return payload


def _user(username="alice"):
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
            [username, "synthetic-original-user"],
        )


def _run(run_id="origin-round"):
    _user()
    context = build_research_context(run_id=run_id, now=datetime(2026, 10, 20, 18, tzinfo=UTC))
    create_research_run("alice", "", "贵州茅台 2024 年报原文核验", context)


def _call(call_id="origin-call"):
    return {
        "type": "tool_use",
        "id": call_id,
        "name": "source_original",
        "input": {"query": "贵州茅台 2024年年度报告", "contains": TARGET},
    }


def _ingest(run_id="origin-round", payload=None, call_id="origin-call"):
    return ingest_original_document("alice", run_id, _call(call_id), payload or _fetch_payload())


def _passage_id(run_id, page):
    stored = read_research_run("alice", run_id)
    return next(passage["passage_id"] for passage in stored["passages"] if passage["locator"]["page"] == page)


def _claim(source_locator, run_id="origin-round", **extra):
    claim = {
        "kind": "fact",
        "statement": "2024年营业收入为170,899,152,276.34元",
        "supports": [_passage_id(run_id, TARGET_PAGE)],
        "value": TARGET,
        "unit": "元",
        "period": "2024",
        "verification": "verified_source",
        "source_locator": source_locator,
    }
    claim.update(extra)
    return claim


def _bare_verified_claim(**extra):
    """A claim that declares first-hand verification without any stored passage."""

    claim = {
        "kind": "inference",
        "statement": "未取得原文即声称已核验",
        "limitations": ["合成失效条件"],
        "verification": "verified_source",
    }
    claim.update(extra)
    return claim


# ---- origin_source: page extraction and degradation (pure, monkeypatched) -----------------


def test_read_pages_keeps_every_physical_page():
    pages = [
        {"page": index + 1, "text": f"Annual report page {index + 1} | {index + 1} / 25"}
        for index in range(25)
    ]
    pages[4]["text"] = f"Operating revenue {TARGET} CNY | 5 / 25"
    result = origin_source.read_pages(pdf_bytes([page["text"] for page in pages]))

    assert result["status"] == "ok"
    assert result["page_count"] == 25
    assert [page["page"] for page in result["pages"]] == list(range(1, 26))
    assert TARGET in result["pages"][4]["text"]


def test_fetch_original_locates_number_on_matching_page(monkeypatch):
    payload = _fetch_payload()
    monkeypatch.setattr(
        origin_source,
        "search_announcement",
        lambda *args, **kwargs: {
            "status": "ok",
            "announcement": payload["announcement"],
            "candidates": [],
            "index": payload["index"],
        },
    )
    raw = pdf_bytes([page["text"] for page in payload["pages"]])
    monkeypatch.setattr(
        origin_source,
        "download_original_pdf",
        lambda url: {
            "status": "ok",
            "body": raw,
            "pdf_sha256": hashlib.sha256(raw).hexdigest(),
            "file_bytes": len(raw),
            "url": PDF_URL,
            "retrieved_at": "2026-10-20T18:00:00+08:00",
        },
    )
    result = origin_source.fetch_original({"query": "贵州茅台 2024年年度报告", "contains": TARGET})

    assert result["status"] == "ok"
    assert result["page_count"] == 3
    assert result["file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert [match["page"] for match in result["matches"]] == [TARGET_PAGE]
    assert TARGET in result["matches"][0]["snippet"]


def test_fetch_original_degrades_when_download_is_waf_html(monkeypatch):
    monkeypatch.setattr(
        origin_source,
        "search_announcement",
        lambda *args, **kwargs: {
            "status": "ok",
            "announcement": _announcement(),
            "candidates": [],
            "index": None,
        },
    )
    monkeypatch.setattr(
        origin_source,
        "download_original_pdf",
        lambda url: {"status": "not_pdf", "detail": "响应不是PDF文件"},
    )
    result = origin_source.fetch_original({"query": "贵州茅台 2024年年度报告"})

    assert result == {"status": "not_pdf", "detail": "响应不是PDF文件"}


def test_fetch_original_rejects_mismatched_date_pair():
    result = origin_source.fetch_original({"query": "贵州茅台 2024年年度报告", "start_date": "2025-01-01"})

    assert result["status"] == "invalid_arguments"


# ---- ledger: original document, page passages and the verified_source gate ----------------


def test_ingest_original_document_stores_original_page_passages():
    _run()
    stored = _ingest()

    document = stored["document"]
    assert document["original_obtained"] is True
    assert document["source_kind"] == "announcement_pdf"
    assert document["access_method"] == "source_original"
    assert document["page_count"] == len(PAGE_TEXTS)
    assert document["file_sha256"] == stored["document"]["retrieved_body_sha256"]
    assert document["content_scope"] == "original_pdf"
    run = read_research_run("alice", "origin-round")
    assert len(run["documents"]) == 1
    passages = run["passages"]
    assert [passage["locator"]["page"] for passage in passages] == [1, 2, 3]
    assert [passage["locator"]["kind"] for passage in passages] == ["pdf_page"] * 3
    for passage in passages:
        assert passage["excerpt_sha256"] == hashlib.sha256(passage["excerpt"].encode()).hexdigest()
        assert passage["page_sha256"] == passage["excerpt_sha256"]
        assert passage["content_scope"] == "original_pdf_page"
    assert TARGET in passages[2]["excerpt"]
    assert document["passage_ids"] == [passage["passage_id"] for passage in passages]


def test_ingest_original_document_replays_without_duplicating_pages():
    _run()
    first = _ingest()
    again = _ingest()

    assert again["document"]["document_id"] == first["document"]["document_id"]
    run = read_research_run("alice", "origin-round")
    assert len(run["documents"]) == 1
    assert len(run["passages"]) == len(PAGE_TEXTS)


def test_ingest_original_document_rejects_non_pdf_payload():
    _run()
    payload = _fetch_payload(status="not_pdf")

    with pytest.raises(HTTPException) as error:
        _ingest(payload=payload)

    assert error.value.status_code == 422
    assert read_research_run("alice", "origin-round")["documents"] == []


def test_claim_with_matching_page_locator_stays_verified_source():
    _run()
    stored = _ingest()
    document_id = stored["document"]["document_id"]

    result = record_claims(
        "alice",
        "origin-round",
        [_claim({"document_id": document_id, "page": TARGET_PAGE})],
    )
    claim = result["claims"][0]
    assert claim["source_locator"]["page"] == TARGET_PAGE
    assert claim["verification"] == "verified_source"
    assert claim["verification_declared"] is None
    assert claim["verification_reason"] is None
    assert "verification_not_supported" not in claim["gaps"]

    revision = finalize_research_run("alice", "origin-round", f"2024年营业收入为{TARGET}元。")
    check = revision["claim_reconciliation"]
    assert check["status"] == "ok"
    assert check["claims_checked"] == 1
    assert check["verified_claim_ratio"] == 1.0


def test_claim_page_without_the_number_is_demoted():
    _run()
    document_id = _ingest()["document"]["document_id"]

    claim = record_claims(
        "alice",
        "origin-round",
        [_claim({"document_id": document_id, "page": EMPTY_PAGE})],
    )["claims"][0]

    assert claim["verification"] == "secondhand"
    assert claim["verification_declared"] == "verified_source"
    assert "verification_not_supported" in claim["gaps"]
    assert claim["verification_reason"]["code"] == "value_not_in_page_text"
    assert str(EMPTY_PAGE) in claim["verification_reason"]["detail"]


def test_claim_unknown_document_is_demoted():
    _run()
    _ingest()

    claim = record_claims(
        "alice",
        "origin-round",
        [_claim({"document_id": "doc_missing", "page": TARGET_PAGE})],
    )["claims"][0]

    assert claim["verification"] == "secondhand"
    assert claim["verification_reason"]["code"] == "document_not_found"


def test_claim_without_source_locator_is_demoted():
    _run()
    _ingest()

    claim = record_claims(
        "alice",
        "origin-round",
        [_claim(None)],
    )["claims"][0]

    assert claim["verification"] == "secondhand"
    assert claim["verification_declared"] == "verified_source"
    assert "verification_not_supported" in claim["gaps"]
    assert claim["verification_reason"]["code"] == "source_locator_missing"


def test_claim_page_out_of_range_is_demoted():
    _run()
    document_id = _ingest()["document"]["document_id"]

    claim = record_claims(
        "alice",
        "origin-round",
        [_claim({"document_id": document_id, "page": 99})],
    )["claims"][0]

    assert claim["verification"] == "secondhand"
    assert claim["verification_reason"]["code"] == "page_out_of_range"


def test_secondhand_claim_does_not_need_an_original_page():
    _run()
    document_id = _ingest()["document"]["document_id"]

    claim = record_claims(
        "alice",
        "origin-round",
        [_claim({"document_id": document_id, "page": EMPTY_PAGE}, verification="secondhand")],
    )["claims"][0]

    assert claim["verification"] == "secondhand"
    assert claim["verification_declared"] is None
    assert "verification_not_supported" not in claim["gaps"]


def test_claim_schema_exposes_optional_source_locator():
    catalog = {row["id"]: row for row in capability_catalog()}
    claim_properties = catalog["research_record_claims"]["input_schema"]["$defs"]["ResearchClaim"][
        "properties"
    ]
    assert "source_locator" in claim_properties
    args = ClaimsArgs.model_validate(
        {
            "claims": [
                {
                    "kind": "fact",
                    "statement": "2024年营业收入为170,899,152,276.34元",
                    "verification": "verified_source",
                    "source_locator": {"document_id": "doc_abc", "page": 5},
                }
            ]
        }
    )
    locator = args.model_dump()["claims"][0]["source_locator"]
    assert locator == {"document_id": "doc_abc", "page": 5, "section": None, "table": None}


# ---- model-facing guidance: original text is the only verified_source path -------------


def test_original_tool_description_marks_the_only_verified_source_path():
    catalog = {row["id"]: row for row in T.native_catalog()}

    description = catalog["source_original"]["description"]
    assert "唯一路径" in description
    assert "verified_source" in description
    assert "财务数字" in description
    assert "必须先调用本工具" in description
    assert "original_required" in description

    # Discovery snippets and excerpt reads must not read like the original path.
    assert "只是线索" in catalog["source_find"]["description"]
    assert "不能直接登记" in catalog["source_find"]["description"]
    assert "不会登记原文文档" in catalog["source_read"]["description"]


def test_system_prompt_requires_original_before_search_for_financial_numbers():
    prompt = system_prompt(build_research_context(run_id="prompt-round"), "company", search_available=True)

    assert "唯一路径" in prompt
    assert "必须先取得原文" in prompt
    assert "只是线索" in prompt
    assert "不能直接登记为 claim 来源" in prompt
    assert "original_required" in prompt


def test_verified_source_is_rejected_without_any_original_document():
    _run("origin-empty")

    with pytest.raises(OriginalEvidenceRequired) as rejected:
        record_claims("alice", "origin-empty", [_bare_verified_claim()])

    assert rejected.value.error_code == "original_required"
    assert "source_original" in rejected.value.detail
    assert read_research_run("alice", "origin-empty")["claims"] == []


def test_verified_source_rejection_rolls_back_the_whole_claim_batch():
    _run("origin-empty-batch")

    with pytest.raises(OriginalEvidenceRequired):
        record_claims(
            "alice",
            "origin-empty-batch",
            [_bare_verified_claim(verification="secondhand"), _bare_verified_claim()],
        )

    assert read_research_run("alice", "origin-empty-batch")["claims"] == []


# ---- native tool: source_original persists the original run document ----------------------


@pytest.fixture
def original_run(test_user):
    user = test_user["username"]
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('origin-owned',?)", [user])
    research = build_research_context(run_id="origin-tool-round")
    create_research_run(user, "origin-owned", "合成原文研究", research)
    manager = Manager(max_workers=2)
    parent = manager.admit("agent", persist_required=False)
    tracker = EvidenceTracker(research["run_id"])
    with cap.native_run_context(user, "origin-owned", research["run_id"], parent_lease=parent) as context:
        tools = T.NativeTools(context, parent, tracker)
        yield SimpleNamespace(manager=manager, parent=parent, tracker=tracker, tools=tools, context=context)
    parent.release()
    manager._pool.shutdown(wait=True, cancel_futures=False)


async def test_source_original_tool_persists_pages_and_matches(original_run, monkeypatch):
    monkeypatch.setattr(T, "_original_fetch", lambda arguments: _fetch_payload())

    result = await original_run.tools.execute(
        "source_original",
        {"query": "贵州茅台 2024年年度报告", "contains": TARGET},
        "origin-tool-call",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["page_count"] == len(PAGE_TEXTS)
    assert len(data["pages"]) == len(PAGE_TEXTS)
    assert [match["page"] for match in data["matches"]] == [TARGET_PAGE]
    assert TARGET in data["matches"][0]["snippet"]
    assert result["call"]["status"] == "completed"
    stored = read_research_run(original_run.context["username"], original_run.context["run_id"])
    assert len(stored["documents"]) == 1
    document = stored["documents"][0]
    assert document["document_id"] == data["document_id"]
    assert document["original_obtained"] is True
    assert len(stored["passages"]) == len(PAGE_TEXTS)


async def test_source_original_tool_degrades_on_non_pdf_response(original_run, monkeypatch):
    monkeypatch.setattr(
        T,
        "_original_fetch",
        lambda arguments: {"status": "not_pdf", "detail": "响应不是PDF文件"},
    )

    result = await original_run.tools.execute(
        "source_original", {"query": "贵州茅台 2024年年度报告"}, "origin-tool-degraded"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "source_unavailable"
    assert result["data"]["status"] == "not_pdf"
    stored = read_research_run(original_run.context["username"], original_run.context["run_id"])
    assert stored["documents"] == []


async def test_native_claim_tool_surfaces_original_required_without_original_document(original_run):
    result = await original_run.tools.execute(
        "research_record_claims",
        {"claims": [_bare_verified_claim()]},
        "origin-claim-no-original",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "original_required"
    assert "source_original" in result["error"]["message"]
    stored = read_research_run(original_run.context["username"], original_run.context["run_id"])
    assert stored["claims"] == []


async def test_native_original_then_matching_claim_keeps_verified_source(original_run, monkeypatch):
    monkeypatch.setattr(T, "_original_fetch", lambda arguments: _fetch_payload())
    username = original_run.context["username"]
    run_id = original_run.context["run_id"]

    original = await original_run.tools.execute(
        "source_original", {"query": "贵州茅台 2024年年度报告", "contains": TARGET}, "origin-claim-source"
    )
    assert original["ok"] is True
    document_id = original["data"]["document_id"]
    passage = next(
        passage
        for passage in read_research_run(username, run_id)["passages"]
        if passage["locator"]["page"] == TARGET_PAGE
    )

    result = await original_run.tools.execute(
        "research_record_claims",
        {
            "claims": [
                {
                    "kind": "fact",
                    "statement": f"2024年营业收入为{TARGET}元",
                    "supports": [passage["passage_id"]],
                    "value": TARGET,
                    "unit": "元",
                    "period": "2024",
                    "verification": "verified_source",
                    "source_locator": {"document_id": document_id, "page": TARGET_PAGE},
                }
            ]
        },
        "origin-claim-after-original",
    )

    assert result["ok"] is True
    claim = result["data"]["claims"][0]
    assert claim["verification"] == "verified_source"
    assert claim["verification_declared"] is None
    assert claim["verification_reason"] is None
    assert "verification_not_supported" not in claim["gaps"]

    revision = finalize_research_run(username, run_id, f"2024年营业收入为{TARGET}元。")
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 1.0


# ---- excerpt review sees the server-fetched original pages ------------------------------


def test_model_review_accepts_server_fetched_original_pdf_pages():
    # Production run e671f546 had every claim supported by original_pdf_page
    # passages, but the review packet only accepted tool_returned_text and
    # prepared nothing (eligible_excerpts_missing), leaving the run gap
    # model_review_incomplete without spending a review call.
    _run("origin-review")
    document_id = _ingest("origin-review")["document"]["document_id"]
    record_claims(
        "alice",
        "origin-review",
        [
            _claim(
                {"document_id": document_id, "page": TARGET_PAGE},
                run_id="origin-review",
                kind="inference",
                limitations=["合成失效条件"],
            )
        ],
    )

    candidate = model_review.prepare("alice", "origin-review")

    assert candidate["status"] == "ready"
    (claim_entry,) = candidate["packet"]["claims"]
    (source,) = claim_entry["sources"]
    assert source["role"] == "supports"
    assert source["passage_id"] == _passage_id("origin-review", TARGET_PAGE)
    assert source["content_scope"] == "server_fetched_excerpt_only"
    assert TARGET in source["excerpt"]
