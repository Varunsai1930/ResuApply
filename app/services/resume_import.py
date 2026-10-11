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
from collections import Counter

from .resume_draft import heading
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
        return "\n".join(_without_running_lines([_page_text(page) for page in reader.pages[:MAX_PAGES]]))
    except ResumeImportError:
        raise
    # PyPdfError is pypdf's own base error; DependencyError (a missing optional package) is not one.
    except (PyPdfError, DependencyError, ValueError, KeyError, TypeError, AttributeError, RecursionError, NotImplementedError) as exc:
        raise ResumeImportError("This PDF couldn't be read. It may be damaged; try saving it again as a PDF.") from exc


# Eight or more spaces in pypdf's layout text: the gap a layout tool leaves before text set flush
# right (dates, places). Layout text shows an ordinary word space as two or three spaces, and the
# widened spaces of a justified line as up to five.
_GAP = re.compile(r" {8,}")


def _columns(layout: str) -> str | None:
    """A two-column page's text, the left column then the right, or None for a page that isn't one.

    pypdf's layout text puts a column that starts at one position at one character position, with
    a gutter of spaces before it. A position after a wide gap on several lines, with a gutter on
    nearly every line that reaches it and a section heading on both sides of it, is a column: a
    column of dates or places set flush right never holds a heading.
    """
    lines = [line.rstrip() for line in layout.splitlines() if line.strip()]
    starts = Counter(m.start(1) for line in lines for m in re.finditer(r" {4,}(\S)", line))
    for start, count in starts.most_common(3):
        if count < 3:
            break
        reaching = [line for line in lines if len(line) > start]
        if sum(line[start - 2:start] == "  " for line in reaching) < 0.9 * len(reaching):
            continue
        left = [line[:start].strip() for line in lines]
        right = [line[start:].strip() for line in lines]
        if any(heading(part) for part in left if part) and any(heading(part) for part in right if part):
            return "\n".join([part for part in left if part] + [part for part in right if part])
    return None


def _page_text(page) -> str:
    """A page's text in the PDF's reading order, with text set flush right on a line kept apart.

    pypdf's plain text follows the order the PDF draws its text, which keeps columns that are
    drawn one after the other in order, but joins "Sample Analytics" and a flush-right "Remote"
    with one space. Its layout text keeps the gap, but orders everything by position. A plain
    line that the layout text shows with a wide gap is taken with " | " in the gap.
    """
    plain = page.extract_text() or ""
    try:
        layout = page.extract_text(extraction_mode="layout") or ""
    except Exception:  # only a refinement: the plain text stands on its own
        return plain
    if (columns := _columns(layout)) is not None:
        return columns
    gapped = {}
    for line in layout.splitlines():
        parts = [part.strip() for part in _GAP.split(line.strip()) if part.strip()]
        if len(parts) > 1:
            gapped.setdefault(" ".join(line.split()), " | ".join(parts))
    return "\n".join(gapped.get(" ".join(line.split()), line) for line in plain.splitlines())


EDGE_LINES = 3  # a running header or footer is among the first or last lines of a page
_PAGE_NUMBER = re.compile(r"(?i)(?:page\s*)?\d{1,3}(?:\s*(?:of|/)\s*\d{1,3})?|-\s*\d{1,3}\s*-")


def _without_running_lines(pages: list[str]) -> list[str]:
    """The pages' text without page numbers or the header and footer repeated on every page.

    In a resume of several pages, "Page 1 of 2" and a running header ("Jordan Example ·
    jordan@example.com") land between entries, where they read as an entry or a skill. A line among
    the first or last lines of two or more pages is running; only its copy at the top of the first
    page is kept, since that is where contact details belong. A one-page resume is left as it is.
    """
    if len(pages) < 2:
        return pages
    split = [[line for line in page.splitlines() if line.strip()] for page in pages]

    def edges(lines: list[str]) -> list[str]:
        return lines[:EDGE_LINES] + lines[-EDGE_LINES:]

    def key(line: str) -> str:
        return " ".join(line.split()).casefold()

    seen: dict[str, int] = {}
    for lines in split:
        for line in {key(line) for line in edges(lines)}:
            seen[line] = seen.get(line, 0) + 1
    running = {line for line, count in seen.items() if count > 1}
    kept = []
    for number, lines in enumerate(split):
        out = []
        for index, line in enumerate(lines):
            at_edge = index < EDGE_LINES or index >= len(lines) - EDGE_LINES
            if at_edge and _PAGE_NUMBER.fullmatch(line.strip()):
                continue
            if at_edge and key(line) in running and not (number == 0 and index < EDGE_LINES):
                continue
            out.append(line)
        kept.append("\n".join(out))
    return kept


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
    return "\n".join(_header_lines(document) + _block_lines(document.element.body, document))


def _header_lines(document) -> list[str]:
    """The first page's header and footer, where Word templates often put the name and contact details.

    They come first, with the lines contact details are read from. The first page's own header is
    used when the document has one; only the first section's are read.
    """
    section = document.sections[0]
    parts = ([section.first_page_header, section.first_page_footer] if section.different_first_page_header_footer
             else [section.header, section.footer])
    lines = []
    for part in parts:
        if not part.is_linked_to_previous:  # in the first section, "linked" means there is none
            lines.extend(line for line in _block_lines(part._element, part) if line.strip())
    return lines


_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _outermost(box, paragraph) -> bool:
    """Whether a text box belongs to the paragraph itself: not Word's fallback copy, nor inside another box."""
    from docx.oxml.ns import qn

    for ancestor in box.iterancestors():
        if ancestor is paragraph:
            return True
        if ancestor.tag in (f"{{{_MC}}}Fallback", qn("w:txbxContent")):
            return False
    return True


def _block_lines(element, parent) -> list[str]:
    """The lines of a body or table cell in document order: paragraphs, tables and content controls.

    Resumes lay out contact details, entry headings and whole columns in tables, so a table is read
    where it stands. A row whose cells each hold one line reads as one line, "Sample Analytics |
    Jun 2024 – Aug 2024"; a layout row whose cells hold blocks (a sidebar beside the main column)
    is read cell by cell. Merged cells repeat their text and are read once.
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    lines: list[str] = []
    for child in element.iterchildren():
        if child.tag == qn("w:p"):
            lines.append(_paragraph_text(Paragraph(child, parent)))
            # A text box anchored in the paragraph (some templates put a sidebar in one). Word saves
            # its text twice, for new and old readers; the copy under mc:Fallback is skipped.
            for box in child.iter(qn("w:txbxContent")):
                if _outermost(box, child):
                    lines.extend(_block_lines(box, parent))
        elif child.tag == qn("w:tbl"):
            for row in Table(child, parent).rows:
                cells: list[list[str]] = []
                seen = set()
                for cell in row.cells:
                    if id(cell._tc) in seen:
                        continue
                    seen.add(id(cell._tc))
                    text = [line for line in _block_lines(cell._tc, cell) if line.strip()]
                    if text and text not in cells:
                        cells.append(text)
                if all(len(text) == 1 for text in cells):
                    lines.append(" | ".join(text[0] for text in cells))
                else:
                    lines.extend(line for text in cells for line in text)
        elif child.tag == qn("w:sdt") and (content := child.find(qn("w:sdtContent"))) is not None:
            lines.extend(_block_lines(content, parent))
    return lines


def _numbered(paragraph) -> bool:
    """Whether Word shows the paragraph as a list item: numbering on the paragraph or in its style chain.

    Word keeps a list's bullet in the numbering, not in the text, so without this every bullet
    reads as a line of its own. Numbering with ID 0 is Word's way of switching it off.
    """
    properties = paragraph._p.pPr
    if properties is not None and properties.numPr is not None:
        number = properties.numPr.numId
        return number is None or number.val != 0
    style = paragraph.style
    while style is not None:
        properties = style.element.pPr
        if properties is not None and properties.numPr is not None:
            return True
        style = style.base_style
    return False


def _paragraph_text(paragraph) -> str:
    text = paragraph.text
    return f"• {text}" if text.strip() and _numbered(paragraph) else text


# Typographic ligatures (from LaTeX and other typesetting) as the letters they join, and soft hyphens
# (invisible unless a line breaks there) removed: "Certiﬁcations" must read as "Certifications".
_LIGATURES = str.maketrans({"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
                            "\ufb05": "st", "\ufb06": "st", "\u00ad": None})


def _tidy(text: str) -> str:
    """Control characters removed, ligatures spelled out, spaces in each line made single, blank runs made one line."""
    text = clean_text(text).translate(_LIGATURES)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.replace("\r", "\n").split("\n")]
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
