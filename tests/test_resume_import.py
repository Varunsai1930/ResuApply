"""Reading a resume file into text, locally (services/resume_import.py)."""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from app.services.resume_import import MAX_BYTES, ResumeImportError, extract_text, file_kind
from tests.resume_files import blank_pdf_bytes, docx_bytes, encrypted_pdf_bytes, pdf_bytes

LINES = ["Jordan Example", "jordan@example.com | +1 555 0100 | Austin, TX", "EXPERIENCE",
         "Software Engineering Intern, Sample Analytics (Jun 2024 - Aug 2024)",
         "- Built a Flask REST API in Python (1,200 users)"]


def test_a_text_pdf_is_read_line_by_line():
    text = extract_text(pdf_bytes(LINES))
    assert [line for line in text.splitlines() if line] == LINES


def test_a_docx_is_read_with_its_tables():
    text = extract_text(docx_bytes(LINES, table=[["Example State University", "2023-08 - 2027-05"]]))
    assert text.splitlines()[:5] == LINES
    assert "Example State University | 2023-08 - 2027-05" in text


def test_files_are_recognized_by_content_not_name():
    assert file_kind(pdf_bytes(LINES)) == "pdf" and file_kind(docx_bytes(LINES)) == "docx"
    other_zip = io.BytesIO()
    with zipfile.ZipFile(other_zip, "w") as archive:
        archive.writestr("content.xml", "<x/>")  # an OpenDocument-like zip, not a Word file
    assert file_kind(other_zip.getvalue()) is None
    assert file_kind(b"{\\rtf1 Jordan}") is None


@pytest.mark.parametrize("data, message", [
    (b"", "The file is empty"),
    (b"x" * (MAX_BYTES + 1), "over 10 MB"),
    (b"\xd0\xcf\x11\xe0 an old .doc file", "Choose a PDF or Word (.docx) file"),
    (b"%PDF-1.4\nnot really a pdf", "This PDF couldn't be read"),
    (b"PK\x03\x04 broken zip", "Choose a PDF or Word (.docx) file"),
])
def test_unreadable_files_say_what_to_do(data, message):
    with pytest.raises(ResumeImportError, match=re.escape(message)):
        extract_text(data)


def test_a_password_protected_pdf_is_refused():
    with pytest.raises(ResumeImportError, match="password-protected"):
        extract_text(encrypted_pdf_bytes(LINES))


def test_a_pdf_encrypted_only_against_copying_is_read():
    assert "Jordan Example" in extract_text(encrypted_pdf_bytes(LINES, password=""))


def test_a_scanned_pdf_has_no_text_to_import():
    with pytest.raises(ResumeImportError, match="scanned or image-only"):
        extract_text(blank_pdf_bytes())


def test_a_docx_that_expands_too_far_is_refused(monkeypatch):
    from app.services import resume_import

    monkeypatch.setattr(resume_import, "MAX_UNZIPPED", 1000)
    with pytest.raises(ResumeImportError, match="too large"):
        extract_text(docx_bytes(LINES))


def test_control_characters_and_extra_spaces_are_removed():
    from app.services.resume_import import _tidy

    text = _tidy("Jordan\x00 Example\x0b  Austin,\tTX\r\n\n\n\n Skills:\u00a0Python ")
    assert text == "Jordan Example\nAustin, TX\n\nSkills: Python"


def aes_pdf_bytes(lines: list[str], password: str = "") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf_bytes(lines))))
    writer.encrypt(password, owner_password="owner", algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_an_aes_pdf_with_an_empty_password_is_read():
    assert [line for line in extract_text(aes_pdf_bytes(LINES)).splitlines() if line] == LINES


def test_an_aes_pdf_with_a_password_asks_for_a_copy_without_one():
    with pytest.raises(ResumeImportError, match="password-protected"):
        extract_text(aes_pdf_bytes(LINES, password="secret"))


def test_any_other_pdf_reader_error_is_reported_as_unreadable(monkeypatch):
    import pypdf
    from pypdf.errors import DependencyError

    def fail(*args, **kwargs):
        raise DependencyError("some optional dependency is missing")

    monkeypatch.setattr(pypdf, "PdfReader", fail)
    with pytest.raises(ResumeImportError, match="couldn't be read"):
        extract_text(pdf_bytes(LINES))
