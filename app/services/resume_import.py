"""Reading a resume file (PDF or DOCX) into text, locally. Nothing is sent anywhere.

Selectable text is preferred; scanned PDF pages can be read with opt-in local OCR.
Every failure is a ``ResumeImportError`` with a message the user can act
on, raised before anything is stored. Files are recognized by their content, never their name.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass

from .resume_draft import has_contact, heading
from .text import tidy_lines

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


def _has_scan_image(page) -> bool:
    """Whether painted images cover a substantial part of the page, without decoding images.

    Scans can have selectable contact details, so text length cannot decide whether their body
    needs OCR. Image drawing matrices distinguish a scanned body from a small logo/headshot.
    Follow Form XObjects with their transforms and bounding boxes; cap recursion/operations.
    """
    from pypdf import mult
    from pypdf.generic import ContentStream

    identity = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    page_box = tuple(float(value) for value in page.cropbox)
    page_area = (page_box[2] - page_box[0]) * (page_box[3] - page_box[1])
    if not math.isfinite(page_area) or page_area <= 0:
        return False
    rectangles = []
    operations_left = 10_000

    def resolve(value):
        return value.get_object() if hasattr(value, "get_object") else value

    def intersection(first, second):
        return (max(first[0], second[0]), max(first[1], second[1]),
                min(first[2], second[2]), min(first[3], second[3]))

    def affine(values):
        values = [float(value) for value in values]
        if len(values) != 6 or not all(math.isfinite(value) for value in values):
            raise ValueError("invalid image matrix")
        return values

    def transformed(box, matrix):
        x0, y0, x1, y1 = box
        points = [(x * matrix[0] + y * matrix[2] + matrix[4],
                   x * matrix[1] + y * matrix[3] + matrix[5])
                  for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        if not all(math.isfinite(value) for point in points for value in point):
            raise ValueError("invalid image bounds")
        return (min(x for x, _ in points), min(y for _, y in points),
                max(x for x, _ in points), max(y for _, y in points))

    def walk(stream, resources, matrix, clip, ancestors=()):
        nonlocal operations_left
        if len(ancestors) > 6:
            raise ResumeImportError("This PDF's image layout is too complex for OCR. Save a simpler PDF, "
                                    "or fill in the profile form instead.")
        if stream is None:
            return
        stream = stream if isinstance(stream, ContentStream) else ContentStream(stream, page.pdf)
        operations = stream.operations
        if not any(operator in (b"Do", b"INLINE IMAGE") for _, operator in operations):
            return  # selectable text without any painted image needs no further OCR inspection
        resources = resolve(resources) or {}
        objects = resolve(resources.get("/XObject", {}))
        saved = []
        path_bounds = None
        for arguments, operator in operations:
            operations_left -= 1
            if operations_left < 0:
                raise ResumeImportError("This PDF's image layout is too complex for OCR. Save a simpler PDF, "
                                        "or fill in the profile form instead.")
            if operator == b"q":
                saved.append((matrix[:], clip))
            elif operator == b"Q" and saved:
                matrix, clip = saved.pop()
            elif operator == b"cm":
                matrix = mult(affine(arguments), matrix)
            elif operator == b"re":
                x, y, width, height = (float(value) for value in arguments)
                rectangle = transformed((x, y, x + width, y + height), matrix)
                path_bounds = rectangle if path_bounds is None else (
                    min(path_bounds[0], rectangle[0]), min(path_bounds[1], rectangle[1]),
                    max(path_bounds[2], rectangle[2]), max(path_bounds[3], rectangle[3]))
            elif operator in (b"W", b"W*") and path_bounds is not None:
                clip = intersection(clip, path_bounds)
            elif operator in (b"n", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
                path_bounds = None
            elif operator in (b"Do", b"INLINE IMAGE"):
                item = resolve(objects.get(arguments[0])) if operator == b"Do" and arguments else None
                subtype = item.get("/Subtype") if item is not None else None
                if subtype == "/Image" or operator == b"INLINE IMAGE":
                    rectangle = intersection(transformed((0, 0, 1, 1), matrix), clip)
                    if rectangle[2] > rectangle[0] and rectangle[3] > rectangle[1]:
                        rectangles.append(rectangle)
                        if len(rectangles) > 256:
                            raise ResumeImportError("This PDF has too many image fragments for OCR. Save a "
                                                    "simpler PDF, or fill in the profile form instead.")
                elif subtype == "/Form" and id(item) not in ancestors:
                    nested_matrix = mult(affine(item.get("/Matrix", identity)), matrix)
                    nested_clip = intersection(transformed(tuple(float(value) for value in item.get("/BBox", page_box)), nested_matrix), clip)
                    walk(item, item.get("/Resources", resources), nested_matrix, nested_clip, (*ancestors, id(item)))

    walk(page.get_contents(), page.get("/Resources", {}), identity, page_box)
    # Union of bounded rectangles, so a repeated logo is not mistaken for a full-page scan.
    area = 0.0
    edges = sorted({edge for rectangle in rectangles for edge in (rectangle[0], rectangle[2])})
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((box[1], box[3]) for box in rectangles if box[0] < right and box[2] > left)
        height = 0.0
        bottom = top = None
        for low, high in intervals:
            if top is None or low > top:
                height += top - bottom if top is not None else 0.0
                bottom, top = low, high
            else:
                top = max(top, high)
        height += top - bottom if top is not None else 0.0
        area += (right - left) * height
    return area >= page_area * 0.4


def _pdf_text(data: bytes, *, use_ocr: bool = False, ocr_pages: list[int] | None = None) -> str:
    from pypdf import PdfReader
    from pypdf.errors import DependencyError, PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Some PDFs are "encrypted" with an empty password only to restrict copying.
            if not reader.decrypt(""):
                raise ResumeImportError("This PDF is password-protected. Save a copy without a password, "
                                        "then import that.")
        pages = list(reader.pages[:MAX_PAGES])
        text = [_page_text(page) for page in pages]
        if use_ocr:
            scanned = [i for i, (page, value) in enumerate(zip(pages, text))
                       if not value.strip() or _has_scan_image(page)]
            if scanned:
                from .resume_ocr import OCRError, recognize_pages

                try:
                    recognized = recognize_pages(data, scanned, max_chars=MAX_CHARS)
                except OCRError as exc:
                    raise ResumeImportError(str(exc)) from exc
                for i in scanned:
                    # Keep sparse selectable headings/stamps verbatim, and add OCR lines only once.
                    known = {line.strip().casefold() for line in text[i].splitlines() if line.strip()}
                    new = [line for line in recognized[i].splitlines() if line.strip().casefold() not in known]
                    text[i] = "\n".join([text[i], *new]).strip()
                if ocr_pages is not None:
                    ocr_pages.extend(i + 1 for i in scanned)
        return "\n".join(_without_running_lines(text))
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
    nearly every line between the first and last of those, and a section heading on both sides of
    it, is a column: a column of dates or places set flush right never holds a heading. A line
    across the gutter (a centred name, a contact line) is kept whole: above the columns when it
    comes before them, below when after, and in the left column when between.
    """
    lines = [line.rstrip() for line in layout.splitlines() if line.strip()]
    starts = Counter(m.start(1) for line in lines for m in re.finditer(r" {4,}(\S)", line))
    for start, count in starts.most_common(3):
        if count < 3:
            break

        def crosses(line: str) -> bool:
            return len(line) > start and line[start - 2:start] != "  "

        rows = [i for i, line in enumerate(lines) if len(line) > start and not crosses(line)]
        first, last = rows[0], rows[-1]
        reaching = [line for line in lines[first:last + 1] if len(line) > start]
        if len(rows) < 0.9 * len(reaching):
            continue
        top, left, right, bottom = [], [], [], []
        for index, line in enumerate(lines):
            if crosses(line):
                (top if index < first else bottom if index > last else left).append(line.strip())
            else:
                left.append(line[:start].strip())
                right.append(line[start:].strip())
        if any(heading(part) for part in left if part) and any(heading(part) for part in right if part):
            return "\n".join(part for part in top + left + right + bottom if part)
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
_PAGE_NUMBER = re.compile(r"(?i)(?:page\s*)?(\d{1,3})(?:\s*(?:of|/)\s*(\d{1,3}))?|-\s*(\d{1,3})\s*-")


def _page_number(line: str, pages: int) -> bool:
    """Whether a line is a page number of a document this many pages long: "2", "Page 1 of 2", "- 2 -".

    A number that doesn't fit the document, such as the date "06/22" in a two-page resume, is text.
    """
    if not (m := _PAGE_NUMBER.fullmatch(line.strip())):
        return False
    number = int(m.group(1) or m.group(3))
    total = int(m.group(2)) if m.group(2) else pages
    return 1 <= number <= total <= pages


def _without_running_lines(pages: list[str]) -> list[str]:
    """The pages' text without page numbers or the header and footer repeated on every page.

    In a resume of several pages, "Page 1 of 2" and a running header ("Jordan Example ·
    jordan@example.com") land between entries, where they read as an entry or a skill. A line among
    the first lines of two or more pages is a running header, and one among the last lines of two or
    more pages a running footer; a line that ends one page and starts the next is neither. Only the
    first page's copy of a running header is kept, at the top where contact details are read, and a
    running footer that holds contact details is kept once, moved there. A one-page resume is left as
    it is.
    """
    if len(pages) < 2:
        return pages
    split = [[line for line in page.splitlines() if line.strip()] for page in pages]

    def key(line: str) -> str:
        return " ".join(line.split()).casefold()

    tops = Counter(k for lines in split for k in {key(line) for line in lines[:EDGE_LINES]})
    bottoms = Counter(k for lines in split for k in {key(line) for line in lines[-EDGE_LINES:]})
    moved: list[str] = []
    kept = []
    for number, lines in enumerate(split):
        out = []
        for index, line in enumerate(lines):
            top, bottom = index < EDGE_LINES, index >= len(lines) - EDGE_LINES
            if (top or bottom) and _page_number(line, len(pages)):
                continue
            if top and tops[key(line)] > 1 and number:
                continue
            if bottom and bottoms[key(line)] > 1 and not (top and number == 0 and tops[key(line)] > 1):
                if has_contact(line) and key(line) not in {key(m) for m in moved}:
                    moved.append(line)
                continue
            out.append(line)
        kept.append("\n".join(out))
    kept[0] = "\n".join([*moved, kept[0]]) if moved else kept[0]
    return kept


def _docx_text(data: bytes) -> str:
    from docx.opc.exceptions import PackageNotFoundError
    from docx.oxml.exceptions import InvalidXmlError
    from lxml.etree import XMLSyntaxError

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parts = archive.infolist()
        if sum(info.file_size for info in parts) > MAX_UNZIPPED:
            raise ResumeImportError("This Word file is too large to read.")
        if any(info.flag_bits & 1 for info in parts):
            raise ResumeImportError("This Word file is password-protected. Save a copy without a password, "
                                    "then import that.")
    try:
        return _docx_lines(data)
    # Damaged XML, a garbled compressed part, or a compression method zipfile can't read.
    except (PackageNotFoundError, XMLSyntaxError, InvalidXmlError, zlib.error, NotImplementedError, KeyError, IndexError, ValueError,
            zipfile.BadZipFile) as exc:
        raise ResumeImportError("This Word file couldn't be read. Try saving it again as .docx.") from exc


def _docx_lines(data: bytes) -> str:
    from docx import Document
    from docx.oxml.ns import qn

    document = Document(io.BytesIO(data))
    # Well-formed XML can still lack the document structure python-docx expects.
    body = document.element.find(qn("w:body"))
    if document.element.tag != qn("w:document") or body is None:
        raise ValueError("The Word document root or body is missing.")
    return "\n".join(_header_lines(document) + _block_lines(body, document))


def _header_lines(document) -> list[str]:
    """The first page's header, and the contact details in its footer, where Word templates often put them.

    They come first, with the lines contact details are read from. A footer line counts only when it
    holds a contact detail (email, phone, link or place): the rest of a footer is page numbers and
    boilerplate such as "References Available Upon Request", which would otherwise be read as the
    name. The first page's own header and footer are used when the document has them; only the first
    section's are read.
    """
    if not len(document.sections):  # a document may leave its section settings out
        return []
    section = document.sections[0]
    first = section.different_first_page_header_footer
    header, footer = ((section.first_page_header, section.first_page_footer) if first
                      else (section.header, section.footer))
    lines = []
    for part, keep in ((header, lambda line: True), (footer, has_contact)):
        if not part.is_linked_to_previous:  # in the first section, "linked" means there is none
            lines.extend(line for line in _block_lines(part._element, part) if line.strip() and keep(line))
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
    style, seen = paragraph.style, set()
    # A style may be based on itself, or on a style that leads back to it: each is looked at once.
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
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
    return tidy_lines(text.translate(_LIGATURES))[:MAX_CHARS]


@dataclass(frozen=True)
class ResumeText:
    text: str
    ocr_pages: tuple[int, ...] = ()


def read_resume(data: bytes, *, use_ocr: bool = False) -> ResumeText:
    """Read text and its OCR provenance, without saving the file or profile."""
    if not data:
        raise ResumeImportError("The file is empty. Choose your resume as a PDF or Word (.docx) file.")
    if len(data) > MAX_BYTES:
        raise ResumeImportError(TOO_LARGE)
    kind = file_kind(data)
    if kind is None:
        raise ResumeImportError("Choose a PDF or Word (.docx) file. Older .doc files, images and other formats "
                                "can't be read; save the resume as PDF or .docx first.")
    ocr_pages: list[int] = []
    text = _tidy(_pdf_text(data, use_ocr=use_ocr, ocr_pages=ocr_pages) if kind == PDF else _docx_text(data))
    if len(text) < MIN_CHARS:
        if ocr_pages:
            raise ResumeImportError("OCR could not find enough readable text. Try a clearer scan, a PDF with "
                                    "selectable text, or fill in the profile form instead.")
        raise ResumeImportError("No text could be found in this file. A scanned or image-only resume can't be "
                                "read without OCR; select 'Use OCR for scanned pages', or fill in the profile form instead.")
    return ResumeText(text, tuple(ocr_pages))


def extract_text(data: bytes, *, use_ocr: bool = False) -> str:
    """The text of a resume file, or ``ResumeImportError`` with what to do instead."""
    return read_resume(data, use_ocr=use_ocr).text
