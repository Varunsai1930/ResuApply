"""Exercise Chromium's actual PDF output, including browser margins set to None.

Like autofill's browser tests, these skip when the optional Chromium isn't installed.
PDFium is already a runtime dependency, so no extra PDF tooling is needed.
"""

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from app.services import profile, resume
from app.templating import STATIC_DIR, templates


@pytest.fixture(scope="module")
def print_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("Optional Chromium is not installed")
        browser = runtime.chromium.launch()
        yield browser
        browser.close()


def pdf_geometry(content):
    """Return selectable text and character extents for every printed page."""
    result = []
    with pdfium.PdfDocument(content) as document:
        for index in range(len(document)):
            page = document[index]
            text = page.get_textpage()
            try:
                boxes = [text.get_charbox(i) for i in range(text.count_chars())]
                boxes = [box for box in boxes if box[2] > box[0] and box[3] > box[1]]
                assert boxes, "The resume produced an empty page"
                result.append((text.get_text_bounded(), min(b[0] for b in boxes),
                               page.get_width() - max(b[2] for b in boxes)))
            finally:
                text.close()
                page.close()
    return result


@pytest.mark.parametrize("layout", ["classic", "modern", "compact"])
@pytest.mark.parametrize("browser_margins", ["default", "none"])
def test_pdf_keeps_gutters_and_content_on_every_page(session, sample_profile, print_browser, layout, browser_margins):
    profile.save(session, sample_profile)
    candidate = profile.get_candidate(session)
    rendered = resume.render_context(candidate.profile, resume.profile_draft(candidate.profile))
    document = templates.env.get_template("_resume_document.html").render(
        rendered=rendered, selected_layout=layout,
    )
    css = (STATIC_DIR / "resume.css").read_text()
    if browser_margins == "none":
        css += "\n@page { margin: 0; }"
    # A narrow screen must still print a full-width A4 page, not the mobile layout.
    page = print_browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.set_content(f'<style>{css}</style><body class="resume-print-page">{document}</body>')
        short = pdf_geometry(page.pdf(prefer_css_page_size=True))
        assert len(short) == 1
        page.evaluate("""() => {
            const entry = document.querySelector('.resume-entry');
            for (let n = 0; n < 12; n++) entry.parentElement.append(entry.cloneNode(true));
        }""")
        long = pdf_geometry(page.pdf(prefer_css_page_size=True))
        assert len(long) > 1
        for pages in (short, long):
            for _, left, right in pages:
                # 18 mm = 51 pt; tolerate glyph bearings and Chromium rounding.
                assert left >= 49, f"Left gutter lost: {left:.1f} pt"
                assert right >= 49, f"Right gutter lost: {right:.1f} pt"
            text = " ".join(entry[0] for entry in pages)
            for value in ("Jordan Example", "Sample Analytics", "TaskBot", "Example State University", "PostgreSQL"):
                assert value in text
    finally:
        page.close()
