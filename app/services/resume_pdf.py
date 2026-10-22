"""Local A4 PDF export and previews rasterized from those exact PDF bytes.

The renderer never visits an HTTP endpoint, runs document scripts or fetches URLs.
Playwright stays optional; ordinary print and Word export still work without it.
"""

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from threading import RLock

import pypdfium2 as pdfium

from ..templating import STATIC_DIR, templates
from .resume_layouts import get_layout


class PDFError(ValueError):
    """A readable, recoverable PDF rendering failure."""


SETUP_MESSAGE = (
    "Direct PDF download needs the local browser add-on. Install it with "
    "`pip install -r requirements-autofill.txt`, then run `python -m playwright install chromium`. "
    "You can still download Word or use your browser's Print command."
)
_lock = RLock()  # PDFium and each synchronous Playwright session are used serially.
_cache: OrderedDict[str, 'PDFDocument'] = OrderedDict()
MAX_CACHE_BYTES = 24 * 1024 * 1024


@dataclass(frozen=True)
class PDFDocument:
    content: bytes
    pages: int
    version: str

    def image(self, page_number: int) -> bytes:
        if not 1 <= page_number <= self.pages:
            raise IndexError("Preview page not found.")
        with _lock, pdfium.PdfDocument(self.content) as document:
            page = document[page_number - 1]
            bitmap = None
            try:
                bitmap = page.render(scale=2)
                image = bitmap.to_pil()
                result = BytesIO()
                image.save(result, format="PNG")
                image.close()
                return result.getvalue()
            finally:
                if bitmap is not None:
                    bitmap.close()
                page.close()


def _build(html: str, executable: str | None) -> bytes:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise PDFError(SETUP_MESSAGE) from exc
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, executable_path=executable, timeout=15_000)
            try:
                context = browser.new_context(java_script_enabled=False, service_workers="block")
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(20_000)
                page.set_content(html, wait_until="load", timeout=20_000)
                return page.pdf(format="A4", prefer_css_page_size=True, print_background=True,
                                display_header_footer=False, tagged=True)
            finally:
                browser.close()
    except Error as exc:
        if "Executable doesn't exist" in str(exc):
            raise PDFError(SETUP_MESSAGE) from exc
        raise PDFError("The PDF could not be prepared. Try again, download Word, or use your browser's Print command.") from exc


def render_html(document_html: str, executable: str | None = None) -> PDFDocument:
    """Only call with the app's escaped resume template or a frozen app-generated snapshot."""
    css = (STATIC_DIR / "resume.css").read_text(encoding="utf-8")
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<style>{css}</style></head><body class="resume-print-page">{document_html}</body></html>')
    version = sha256((html + "\0" + (executable or "")).encode("utf-8")).hexdigest()
    with _lock:
        if version in _cache:
            _cache.move_to_end(version)
            return _cache[version]
        content = _build(html, executable)
        try:
            with pdfium.PdfDocument(content) as pdf:
                pages = len(pdf)
        except pdfium.PdfiumError as exc:
            raise PDFError("The PDF could not be read. Try again, or download Word.") from exc
        result = PDFDocument(content, pages, version)
        if len(content) <= MAX_CACHE_BYTES:
            _cache[version] = result
            while len(_cache) > 4 or sum(len(value.content) for value in _cache.values()) > MAX_CACHE_BYTES:
                _cache.popitem(last=False)
        return result


def export_resume(rendered: dict, layout: str = "classic", executable: str | None = None) -> PDFDocument:
    key = get_layout(layout).key
    html = templates.env.get_template("_resume_document.html").render(rendered=rendered, selected_layout=key)
    return render_html(html, executable)
