"""Actual bounded child parser: page provenance, encrypted and invalid PDFs."""

import io
import subprocess

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from server.agent import pdf_text


def pdf(*, pages=1, encrypted=False):
    writer = PdfWriter()
    for index in range(pages):
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
        content.set_data(
            f"BT /F1 12 Tf 30 300 Td (Synthetic disclosure page {index + 1}: Revenue 120.) Tj ET".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(content)
    if encrypted:
        writer.encrypt("synthetic-parser-fixture")
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_pdf_actual_text_pages_and_explicit_truncation():
    value = pdf_text.extract_pdf(pdf(pages=3), first_page=2, page_count=1)
    assert value["status"] == "ok" and value["page_count"] == 3 and value["truncated"]
    assert value["pages"][0]["page"] == 2 and "Revenue 120" in value["pages"][0]["text"]


def test_selected_page_after_initial_window_and_out_of_range_are_distinct():
    raw = pdf(pages=15)
    value = pdf_text.extract_pdf(raw, first_page=12, page_count=2)
    assert value["status"] == "ok" and value["page_count"] == 15 and value["truncated"]
    assert [page["page"] for page in value["pages"]] == [12, 13]
    assert "page 12:" in value["pages"][0]["text"]
    assert pdf_text.extract_pdf(raw, first_page=16) == {
        "status": "pdf_page_out_of_range",
        "page_count": 15,
    }


def test_encrypted_and_invalid_pdf_never_become_empty_success():
    assert pdf_text.extract_pdf(pdf(encrypted=True))["status"] == "pdf_encrypted"
    assert pdf_text.extract_pdf(b"%PDF-invalid")["status"] == "pdf_invalid"
    assert pdf_text.extract_pdf(b"x" * (pdf_text.MAX_PDF_BYTES + 1))["status"] == "pdf_invalid"


def test_timed_out_parser_is_killed_and_reaped_without_retry(monkeypatch):
    observed = []

    class Process:
        returncode = None

        def communicate(self, raw=None, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("synthetic parser", timeout)
            observed.append("reaped-output")
            return b"", b""

        def kill(self):
            observed.append("kill")
            self.returncode = -9

        def poll(self):
            return self.returncode

        def wait(self):
            observed.append("wait")

    def launch(command, **kwargs):
        assert command[1:4] == ["-I", "-B", "-S"]
        assert "HOME" not in kwargs["env"] and kwargs["close_fds"]
        observed.append("launch")
        return Process()

    monkeypatch.setattr(pdf_text.subprocess, "Popen", launch)
    assert pdf_text.extract_pdf(b"%PDF-synthetic")["status"] == "pdf_timeout"
    assert observed == ["launch", "kill", "reaped-output", "wait"]
