"""Local A4 PDF export and previews rasterized from those exact PDF bytes.

The renderer never visits an HTTP endpoint, runs document scripts or fetches URLs.
Playwright stays optional; ordinary print and Word export still work without it.
"""

from collections import OrderedDict
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
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
_lock = RLock()  # PDFium calls are used serially.
_render_lock = RLock()  # Coalesce identical exports without blocking cached page images.
_cache_lock = RLock()
_cache: OrderedDict[str, 'PDFDocument'] = OrderedDict()
_images: OrderedDict[tuple[str, int], bytes] = OrderedDict()
MAX_CACHE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class PDFDocument:
    content: bytes
    pages: int
    version: str
    text_characters: int = 0
    warnings: tuple[str, ...] = ()

    def image(self, page_number: int) -> bytes:
        if not 1 <= page_number <= self.pages:
            raise IndexError("Preview page not found.")
        with _lock:
            key = (self.version, page_number)
            if key in _images:
                _images.move_to_end(key)
                return _images[key]
            content = self._image(page_number)
            if len(content) <= MAX_IMAGE_BYTES:
                _images[key] = content
                while len(_images) > 12 or sum(map(len, _images.values())) > MAX_IMAGE_BYTES:
                    _images.popitem(last=False)
            return content

    def _image(self, page_number: int) -> bytes:
        with pdfium.PdfDocument(self.content) as document:
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


def _readability(pdf) -> tuple[int, tuple[str, ...]]:
    characters = 0
    clipped = False
    unreadable = False
    for page in pdf:
        try:
            width, height = page.get_size()
            with closing(page.get_textpage()) as text:
                value = text.get_text_range()
                characters += len(value.strip())
                unreadable |= '\ufffd' in value
                for index in range(text.count_chars()):
                    left, bottom, right, top = text.get_charbox(index)
                    if (right > left and top > bottom and
                            (left < -1 or bottom < -1 or right > width + 1 or top > height + 1)):
                        clipped = True
        finally:
            page.close()
    warnings = []
    if not characters:
        warnings.append("No searchable text was found. Check the PDF before submitting.")
    if unreadable:
        warnings.append("Some PDF text could not be read. Check for missing characters.")
    if clipped:
        warnings.append("Some text extends beyond a page edge. Try another layout or shorten the content.")
    return characters, tuple(warnings)


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


def _source(document_html, executable):
    css = (STATIC_DIR / "resume.css").read_text(encoding="utf-8")
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<style>{css}</style></head><body class="resume-print-page">{document_html}</body></html>')
    version = sha256((html + "\0" + (executable or "")).encode("utf-8")).hexdigest()
    return html, version


def _cached(version):
    with _cache_lock:
        if version in _cache:
            _cache.move_to_end(version)
            return _cache[version]


def _render(html, version, executable):
    result = _cached(version)
    if result is not None:
        return result
    with _render_lock:
        result = _cached(version)
        if result is not None:
            return result
        content = _build(html, executable)
        try:
            with _lock, pdfium.PdfDocument(content) as pdf:
                pages = len(pdf)
                characters, warnings = _readability(pdf)
        except pdfium.PdfiumError as exc:
            raise PDFError("The PDF could not be read. Try again, or download Word.") from exc
        result = PDFDocument(content, pages, version, characters, warnings)
        with _cache_lock:
            if len(content) <= MAX_CACHE_BYTES:
                _cache[version] = result
                while len(_cache) > 4 or sum(len(value.content) for value in _cache.values()) > MAX_CACHE_BYTES:
                    _cache.popitem(last=False)
        return result


def render_html(document_html: str, executable: str | None = None) -> PDFDocument:
    """Only call with the app's escaped resume template or a frozen app-generated snapshot."""
    html, version = _source(document_html, executable)
    return _render(html, version, executable)


class PreviewQueue:
    """One bounded worker per application; workers only receive immutable document text."""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="resume-preview")
        self._pending = OrderedDict()
        self._lock = RLock()

    def request(self, document_html, executable=None):
        html, version = _source(document_html, executable)
        cached = _cached(version)
        if cached is not None:
            return cached
        with self._lock:
            if version not in self._pending:
                while len(self._pending) >= 4:
                    completed = next((key for key, value in self._pending.items() if value.done()), None)
                    if completed is None:
                        raise PDFError("Other previews are still preparing. Retry in a moment.")
                    del self._pending[completed]
                self._pending[version] = self._pool.submit(_render, html, version, executable)
            future = self._pending[version]
            self._pending.move_to_end(version)
            if not future.done():
                return None
            try:
                return future.result()
            except Exception:
                del self._pending[version]  # A later explicit retry can render again.
                raise

    def shutdown(self):
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._pending.clear()


def export_resume(rendered: dict, layout: str = "classic", executable: str | None = None,
                  queue: PreviewQueue | None = None) -> PDFDocument | None:
    key = get_layout(layout).key
    html = templates.env.get_template("_resume_document.html").render(rendered=rendered, selected_layout=key)
    return queue.request(html, executable) if queue is not None else render_html(html, executable)
