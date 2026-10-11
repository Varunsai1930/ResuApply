"""Reading a resume file (PDF or DOCX) into text, locally. Nothing is sent anywhere.

Only text-based PDFs can be read: a scanned or image-only PDF has no text, and OCR is
deferred (PLAN.md §4). Every failure is a ``ResumeImportError`` with a message the user can act
on, raised before anything is stored. Files are recognized by their content, never their name.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib

from .text import clean_text

MAX_BYTES = 10 * 1024 * 1024  # an upload larger than this is refused unread
MAX_PAGES = 30  # a resume longer than this is not a resume; only these pages are read
MAX_UNZIPPED = 50 * 1024 * 1024  # a DOCX that expands past this is refused (a zip bomb)
MAX_CHARS = 100_000  # extracted text beyond this is cut
MIN_CHARS = 40  # less text than this means a scanned or image-only file

PDF, DOCX = "pdf", "docx"
TOO_LARGE = f"The file is over {MAX_BYTES // (1024 * 1024)} MB. Choose a smaller PDF or Word file."


class ResumeImportError(Exception):
    """The file can't be read into text. Nothing was changed."""


def file_kind(data: bytes) -> str | None:
    """PDF or DOCX, recognized by the file's content; None for anything else."""
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return DOCX if "word/document.xml" in archive.namelist() else None
        except zipfile.BadZipFile:
            return None
    return None


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import DependencyError, PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Some PDFs are "encrypted" with an empty password only to restrict copying.
            if not reader.decrypt(""):
                raise ResumeImportError("This PDF is password-protected. Save a copy without a password, "
                                        "then import that.")
        return "\n".join(page.extract_text() or "" for page in reader.pages[:MAX_PAGES])
    except ResumeImportError:
        raise
    # PyPdfError is pypdf's own base error; DependencyError (a missing optional package) is not one.
    except (PyPdfError, DependencyError, ValueError, KeyError, TypeError, AttributeError, RecursionError, NotImplementedError) as exc:
        raise ResumeImportError("This PDF couldn't be read. It may be damaged; try saving it again as a PDF.") from exc


def _docx_text(data: bytes) -> str:
    from docx.opc.exceptions import PackageNotFoundError
    from lxml.etree import XMLSyntaxError

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNZIPPED:
            raise ResumeImportError("This Word file is too large to read.")
    try:
        return _docx_lines(data)
    # Damaged XML, a garbled compressed part, or a compression method zipfile can't read.
    except (PackageNotFoundError, XMLSyntaxError, zlib.error, NotImplementedError, KeyError, ValueError,
            zipfile.BadZipFile) as exc:
        raise ResumeImportError("This Word file couldn't be read. Try saving it again as .docx.") from exc


def _docx_lines(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:  # resumes often lay out dates or contact details in tables
        for row in table.rows:
            cells = []
            for cell in row.cells:
                if cell.text not in cells:  # merged cells repeat their text
                    cells.append(cell.text)
            lines.append(" | ".join(text for text in cells if text.strip()))
    return "\n".join(lines)


def _tidy(text: str) -> str:
    """Control characters removed, spaces in each line made single, blank runs made one line."""
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in clean_text(text).replace("\r", "\n").split("\n")]
    tidy = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return tidy[:MAX_CHARS]


def extract_text(data: bytes) -> str:
    """The text of a resume file, or ``ResumeImportError`` with what to do instead."""
    if not data:
        raise ResumeImportError("The file is empty. Choose your resume as a PDF or Word (.docx) file.")
    if len(data) > MAX_BYTES:
        raise ResumeImportError(TOO_LARGE)
    kind = file_kind(data)
    if kind is None:
        raise ResumeImportError("Choose a PDF or Word (.docx) file. Older .doc files, images and other formats "
                                "can't be read; save the resume as PDF or .docx first.")
    text = _tidy(_pdf_text(data) if kind == PDF else _docx_text(data))
    if len(text) < MIN_CHARS:
        raise ResumeImportError("No text could be found in this file. A scanned or image-only resume can't be "
                                "read; fill in the profile form instead.")
    return text
