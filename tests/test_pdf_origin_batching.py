"""Origin PDF page batching: bounded batches and halving retries on pdf_invalid.

The parser child runs under a two-second CPU budget and re-parses the whole
document for every batch, so a batch size that is fine on a ten-page paper can
get a 143-page annual report killed. These tests pin the production fix: small
batches and a halving retry, so a big but readable report degrades into more,
smaller batches instead of failing as ``pdf_invalid``.
"""

import time

from test_evidence_origin import pdf_bytes

from server.agent import origin_source

# A 143-page report is the size that repeatedly failed in production.
REPORT_PAGES = 143


def _page_text(number: int) -> str:
    return f"Annual report page {number} | revenue {number}.00 CNY"


def test_read_pages_reads_a_143_page_report_in_ten_page_batches(monkeypatch):
    raw = pdf_bytes([_page_text(number) for number in range(1, REPORT_PAGES + 1)])
    calls = []
    real_extract = origin_source.extract_pdf

    def recording_extract(raw_bytes, *, timeout_seconds=5, first_page=1, page_count=10):
        calls.append((first_page, page_count))
        return real_extract(
            raw_bytes,
            timeout_seconds=timeout_seconds,
            first_page=first_page,
            page_count=page_count,
        )

    monkeypatch.setattr(origin_source, "extract_pdf", recording_extract)

    started = time.monotonic()
    result = origin_source.read_pages(raw)
    elapsed = time.monotonic() - started

    assert result["status"] == "ok", result
    assert result["page_count"] == REPORT_PAGES
    # Every physical page survives, in order, with its own text: the failure
    # mode that lost pages to truncation or gaps must stay fixed.
    assert [page["page"] for page in result["pages"]] == list(range(1, REPORT_PAGES + 1))
    assert _page_text(REPORT_PAGES) in result["pages"][-1]["text"]
    # Batches stay at or under the new default, starting with it; the single
    # page re-reads after each full batch are allowed to ask for one page.
    assert calls[0] == (1, origin_source._BATCH_PAGES)
    assert origin_source._BATCH_PAGES == 10
    assert all(page_count <= origin_source._BATCH_PAGES for _, page_count in calls)
    # The synthetic report extracts fast locally; the bound only guards against
    # a batch accidentally growing back toward the whole-document re-parse cost.
    assert elapsed < 30


def test_pdf_invalid_batch_is_retried_at_half_size(monkeypatch):
    total = 12
    calls = []

    def fake_extract(raw_bytes, *, timeout_seconds=5, first_page=1, page_count=10):
        calls.append((first_page, page_count))
        if page_count > 5:
            return {"status": "pdf_invalid"}
        last = min(first_page + page_count - 1, total)
        return {
            "status": "ok",
            "page_count": total,
            "pages": [{"page": number, "text": f"page {number}"} for number in range(first_page, last + 1)],
        }

    monkeypatch.setattr(origin_source, "extract_pdf", fake_extract)

    result = origin_source.read_pages(b"fake")

    assert result["status"] == "ok", result
    assert [page["page"] for page in result["pages"]] == list(range(1, total + 1))
    # The first 10-page batch is rejected, so the same first page is retried at
    # 5 pages before any later page is requested. Each following batch repeats
    # the halving, and the one-page re-reads after a full batch keep working.
    assert calls == [(1, 10), (1, 5), (5, 1), (6, 10), (6, 5), (10, 1), (11, 10), (11, 5)]


def test_pdf_invalid_at_the_floor_fails_the_document(monkeypatch):
    calls = []

    def always_invalid(raw_bytes, *, timeout_seconds=5, first_page=1, page_count=10):
        calls.append((first_page, page_count))
        return {"status": "pdf_invalid"}

    monkeypatch.setattr(origin_source, "extract_pdf", always_invalid)

    result = origin_source.read_pages(b"fake")

    assert result == {"status": "pdf_invalid"}
    # 10 -> 5 -> 2, and no further attempt: two failing pages fail the document
    # rather than silently dropping an unreadable page from the report.
    assert calls == [(1, 10), (1, 5), (1, 2)]


def test_other_extraction_failures_are_not_retried(monkeypatch):
    calls = []

    def encrypted(raw_bytes, *, timeout_seconds=5, first_page=1, page_count=10):
        calls.append((first_page, page_count))
        return {"status": "pdf_encrypted"}

    monkeypatch.setattr(origin_source, "extract_pdf", encrypted)

    result = origin_source.read_pages(b"fake")

    assert result == {"status": "pdf_encrypted"}
    assert calls == [(1, 10)]
