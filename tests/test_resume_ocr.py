"""Opt-in OCR preserves selectable text and bounds local native workers."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter, Transformation

from app.services import resume_import, resume_ocr
from tests.resume_files import blank_pdf_bytes, docx_bytes, encrypted_pdf_bytes, pdf_bytes

LINES = ["Jordan Example", "jordan@example.com", "EXPERIENCE", "Built Python services for a research team."]
TEXT = "\n".join(LINES)


def joined_pdf(*documents):
    writer = PdfWriter()
    for data in documents:
        writer.append(PdfReader(io.BytesIO(data)))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def scanned_pdf(lines=LINES):
    from PIL import Image, ImageDraw, ImageFont

    with Image.new("RGB", (1500, 2000), "white") as image:
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=35)
        for number, line in enumerate(lines):
            draw.text((90, 120 + number * 80), line, fill="black", font=font)
        output = io.BytesIO()
        image.save(output, "PDF", resolution=180)
    return output.getvalue()


def scan_with_selectable_header(*, small_image=False, in_form=False):
    """A real scanned body plus selectable contact details, or a text PDF with a small image."""
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, DecodedStreamObject

    header = "Jordan Example | jordan@example.com | Austin, TX"
    writer = PdfWriter()
    if small_image:
        writer.append(PdfReader(io.BytesIO(pdf_bytes([header, *LINES[2:]]))))
        image_page = PdfReader(io.BytesIO(scanned_pdf())).pages[0]
        writer.pages[0].merge_transformed_page(image_page, Transformation().scale(0.15).translate(450, 500))
    else:
        writer.append(PdfReader(io.BytesIO(scanned_pdf(LINES[2:]))))
        if in_form:
            page = writer.pages[0]
            form = DecodedStreamObject()
            form.set_data(page.get_contents().get_data())
            form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                         NameObject("/BBox"): ArrayObject([FloatObject(value) for value in page.mediabox]),
                         NameObject("/Resources"): page["/Resources"]})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/ScanForm"): writer._add_object(form)})})
            content = DecodedStreamObject()
            content.set_data(b"q /ScanForm Do Q")
            page[NameObject("/Contents")] = writer._add_object(content)
        writer.pages[0].merge_page(PdfReader(io.BytesIO(pdf_bytes([header]))).pages[0])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue(), header


@pytest.mark.parametrize("in_form", [False, True])
def test_scanned_body_is_read_even_with_a_long_selectable_contact_header(monkeypatch, in_form):
    data, header = scan_with_selectable_header(in_form=in_form)
    assert len(header) > resume_import.MIN_CHARS
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: {0: TEXT})
    result = resume_import.read_resume(data, use_ocr=True)
    assert result.ocr_pages == (1,)
    assert header in result.text and "EXPERIENCE" in result.text and "Built Python services" in result.text


def test_selectable_text_with_a_small_logo_or_headshot_bypasses_ocr(monkeypatch):
    data, header = scan_with_selectable_header(small_image=True)
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: pytest.fail("Small image is not a scanned page"))
    result = resume_import.read_resume(data, use_ocr=True)
    assert result.ocr_pages == () and header in result.text and "Built Python services" in result.text


def test_dense_selectable_text_beside_a_logo_is_not_too_complex_for_ocr(monkeypatch):
    from pypdf.generic import DecodedStreamObject, NameObject

    data, _ = scan_with_selectable_header(small_image=True)
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(data)))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    # Builders that position every glyph emit thousands of text operators on a single page.
    stream.set_data(page.get_contents().get_data() + b"\n" + b"BT 72 60 Td ( ) Tj ET\n" * 4000)
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: pytest.fail("Small image is not a scanned page"))
    result = resume_import.read_resume(output.getvalue(), use_ocr=True)
    assert result.ocr_pages == () and "Built Python services" in result.text


def test_malformed_drawing_operands_keep_a_text_page_when_ocr_is_requested(monkeypatch):
    from pypdf.generic import DecodedStreamObject, NameObject

    data, header = scan_with_selectable_header(small_image=True)
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(data)))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(b"0 0 10 re n\n" + page.get_contents().get_data())  # `re` needs four operands
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: pytest.fail("Text page is not a scan"))
    result = resume_import.read_resume(output.getvalue(), use_ocr=True)
    assert result.ocr_pages == () and header in result.text


def test_a_large_raster_clipped_to_a_small_headshot_bypasses_ocr(monkeypatch):
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(scanned_pdf())))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(b"q 0 0 80 120 re W n\n" + page.get_contents().get_data() + b"\nQ")
    page[NameObject("/Contents")] = writer._add_object(stream)
    page.merge_page(PdfReader(io.BytesIO(pdf_bytes(LINES))).pages[0])
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: pytest.fail("Clipped photo is not a scanned body"))
    result = resume_import.read_resume(output.getvalue(), use_ocr=True)
    assert result.ocr_pages == () and "Built Python services" in result.text


def test_scanned_pages_require_explicit_opt_in(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("OCR must not run without opt-in")

    monkeypatch.setattr(resume_ocr, "recognize_pages", unexpected)
    with pytest.raises(resume_import.ResumeImportError, match="Use OCR for scanned pages"):
        resume_import.extract_text(blank_pdf_bytes())


@pytest.mark.parametrize("data", [pdf_bytes(LINES), docx_bytes(LINES), pdf_bytes([TEXT[:39]])])
def test_text_files_bypass_ocr_even_when_requested(monkeypatch, data):
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: pytest.fail("Selectable text uses no OCR"))
    if data == pdf_bytes([TEXT[:39]]):
        with pytest.raises(resume_import.ResumeImportError, match="No text"):
            resume_import.read_resume(data, use_ocr=True)
    else:
        result = resume_import.read_resume(data, use_ocr=True)
        assert result.text == TEXT and not result.ocr_pages


def test_mixed_pdf_keeps_text_and_ocr_pages_in_original_order(monkeypatch):
    calls = []

    def recognize(data, pages, **limits):
        calls.append((pages, limits))
        return {1: "SCANNED MIDDLE\nCompleted an engineering project in Python."}

    monkeypatch.setattr(resume_ocr, "recognize_pages", recognize)
    result = resume_import.read_resume(joined_pdf(pdf_bytes(LINES), blank_pdf_bytes(), pdf_bytes(["FINAL PAGE", "EDUCATION", "Example University | Engineering | 2020-2024"])), use_ocr=True)
    assert result.text.index("EXPERIENCE") < result.text.index("SCANNED MIDDLE") < result.text.index("FINAL PAGE")
    assert result.ocr_pages == (2,)
    assert calls == [([1], {"max_chars": resume_import.MAX_CHARS})]


def test_sparse_selectable_heading_is_preserved_when_ocr_misreads_it(monkeypatch):
    source = scanned_pdf()
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(source)))
    writer.pages[0].merge_page(PdfReader(io.BytesIO(pdf_bytes(["CONTACT"]))).pages[0])
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: {0: "C0NTACT\n" + TEXT})
    result = resume_import.read_resume(output.getvalue(), use_ocr=True)
    assert "CONTACT\nC0NTACT" in result.text and result.ocr_pages == (1,)


def test_ocr_keeps_existing_upload_password_and_page_limits(monkeypatch):
    called = []
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda data, pages, **kwargs: called.append(pages) or {i: TEXT for i in pages})
    with pytest.raises(resume_import.ResumeImportError, match="over 10 MB"):
        resume_import.extract_text(b"%PDF-" + b"x" * resume_import.MAX_BYTES, use_ocr=True)
    with pytest.raises(resume_import.ResumeImportError, match="password-protected"):
        resume_import.extract_text(encrypted_pdf_bytes(LINES), use_ocr=True)
    monkeypatch.setattr(resume_import, "MAX_PAGES", 2)
    result = resume_import.read_resume(joined_pdf(blank_pdf_bytes(), blank_pdf_bytes(), blank_pdf_bytes()), use_ocr=True)
    assert called == [[0, 1]] and result.ocr_pages == (1, 2)


def test_blank_ocr_result_explains_how_to_proceed(monkeypatch):
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: {0: ""})
    with pytest.raises(resume_import.ResumeImportError, match="OCR could not find enough"):
        resume_import.extract_text(blank_pdf_bytes(), use_ocr=True)


def test_missing_engine_and_busy_imports_have_plain_messages(monkeypatch):
    monkeypatch.setattr(resume_ocr.shutil, "which", lambda name: None)
    with pytest.raises(resume_ocr.OCRError, match="OCR needs Tesseract"):
        resume_ocr.recognize_pages(b"test", [0], max_chars=100)
    assert resume_ocr._SLOT.acquire(blocking=False)
    try:
        with pytest.raises(resume_ocr.OCRError, match="Another resume"):
            resume_ocr.recognize_pages(b"test", [0], max_chars=100)
    finally:
        resume_ocr._SLOT.release()


def fake_workers(monkeypatch, *, fail="", language="eng", text=TEXT, languages_on_stderr=False):
    calls = []
    monkeypatch.setattr(resume_ocr.shutil, "which", lambda name: "fake-tesseract")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["timeout"] <= resume_ocr.TOTAL_SECONDS
        if command[1] == "--list-langs":
            # Older Tesseract writes the list to stderr; it only reaches the file when merged into stdout.
            if not languages_on_stderr or kwargs.get("stderr") is subprocess.STDOUT:
                kwargs["stdout"].write(f"Languages:\n{language}\n".encode())
        elif "--render" in command:
            if fail == "render-timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if fail == "large":
                return subprocess.CompletedProcess(command, 73)
            if fail == "missing-renderer":
                return subprocess.CompletedProcess(command, 74)
            if fail == "crash":
                return subprocess.CompletedProcess(command, -11)
            Path(command[-1]).write_bytes(b"fake image")
        else:
            if fail == "ocr-timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if fail == "ocr-error":
                return subprocess.CompletedProcess(command, 1)
            Path(command[2]).with_suffix(".txt").write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(resume_ocr.subprocess, "run", run)
    return calls


def test_workers_have_deadlines_and_cleanup_without_inheriting_ai_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-must-not-enter-workers")
    calls = fake_workers(monkeypatch, text="A" * 200)
    result = resume_ocr.recognize_pages(b"synthetic pdf", [0, 1], max_chars=100)
    assert result == {0: "A" * 100, 1: ""}
    assert len(calls) == 3
    assert all("OPENROUTER_API_KEY" not in kwargs["env"] for _, kwargs in calls)
    assert all(kwargs["stdin"] == subprocess.DEVNULL for _, kwargs in calls)
    # Only the language list (read from its own bounded file) keeps stderr; worker output is discarded.
    assert calls[0][1]["stderr"] == subprocess.STDOUT
    assert all(kwargs["stderr"] == subprocess.DEVNULL for _, kwargs in calls[1:])
    temporary = Path(calls[1][0][-1]).parent
    assert not temporary.exists()


@pytest.mark.parametrize("failure, expected", [
    ("render-timeout", "took too long"), ("ocr-timeout", "took too long"),
    ("large", "large dimensions"), ("missing-renderer", "renderer is unavailable"),
    ("crash", "could not be rendered"), ("ocr-error", "could not read"),
])
def test_worker_failures_cleanup_and_release_slot(monkeypatch, failure, expected):
    calls = fake_workers(monkeypatch, fail=failure)
    with pytest.raises(resume_ocr.OCRError, match=expected):
        resume_ocr.recognize_pages(b"synthetic pdf", [0], max_chars=100)
    assert not Path(calls[1][0][-1]).parent.exists()
    assert resume_ocr._SLOT.acquire(blocking=False)
    resume_ocr._SLOT.release()


def test_language_list_printed_to_stderr_by_older_tesseract_is_read(monkeypatch):
    fake_workers(monkeypatch, languages_on_stderr=True)
    assert resume_ocr.recognize_pages(scanned_pdf(), [0], max_chars=1000) == {0: TEXT}


def test_missing_english_language_data_is_reported_before_rendering(monkeypatch):
    calls = fake_workers(monkeypatch, language="deu")
    with pytest.raises(resume_ocr.OCRError, match="English language data"):
        resume_ocr.recognize_pages(b"synthetic", [0], max_chars=100)
    assert len(calls) == 1


def test_total_deadline_stops_work_before_starting_another_process(monkeypatch):
    calls = fake_workers(monkeypatch)
    moments = iter([0.0, 0.0, 61.0])
    monkeypatch.setattr(resume_ocr.time, "monotonic", lambda: next(moments))
    with pytest.raises(resume_ocr.OCRError, match="took too long"):
        resume_ocr.recognize_pages(b"synthetic", [0], max_chars=100)
    assert len(calls) == 1


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Optional native Tesseract is not installed")
def test_real_english_scan_is_read_by_local_tesseract():
    result = resume_import.read_resume(scanned_pdf(), use_ocr=True)
    assert result.ocr_pages == (1,)
    assert "Jordan Example" in result.text and "jordan@example.com" in result.text
    assert "Built Python services" in result.text


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Optional native Tesseract is not installed")
@pytest.mark.parametrize("in_form", [False, True])
def test_real_scanned_body_with_selectable_header_is_read_by_tesseract(in_form):
    data, header = scan_with_selectable_header(in_form=in_form)
    result = resume_import.read_resume(data, use_ocr=True)
    assert result.ocr_pages == (1,) and header in result.text
    assert "EXPERIENCE" in result.text and "Built Python services" in result.text


def test_renderer_rejects_extreme_page_dimensions_before_allocating(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=1_000_000, height=1_000_000)
    source = tmp_path / "large.pdf"
    with source.open("wb") as output:
        writer.write(output)
    destination = tmp_path / "page.png"
    assert resume_ocr._render(str(source), 0, str(destination)) == 73
    assert not destination.exists()


def test_renderer_downscales_large_pages_to_pixel_and_edge_bounds(tmp_path):
    from PIL import Image

    writer = PdfWriter()
    writer.add_blank_page(width=3000, height=3000)
    source = tmp_path / "wide.pdf"
    with source.open("wb") as output:
        writer.write(output)
    destination = tmp_path / "page.png"
    assert resume_ocr._render(str(source), 0, str(destination)) == 0
    with Image.open(destination) as image:
        assert image.width * image.height <= resume_ocr.MAX_PIXELS
        assert max(image.size) <= resume_ocr.MAX_EDGE


@pytest.mark.skipif(os.name != "posix", reason="Uses POSIX process existence check; Windows timeout logic is shared")
def test_real_process_timeout_kills_and_reaps_the_direct_worker(tmp_path):
    marker = tmp_path / "pid.txt"
    script = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(resume_ocr.OCRError, match="took too long"):
        resume_ocr._run([sys.executable, "-c", script, str(marker)], deadline=started + 2, limit=0.4)
    assert time.monotonic() - started < 2
    assert marker.is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
