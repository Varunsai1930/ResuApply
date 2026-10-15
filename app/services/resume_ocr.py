"""Opt-in, local English OCR. Renderers and Tesseract run as bounded child processes.

PDFium is loaded only in the rendering child: a corrupt native document cannot crash the
web server. The parent owns each renderer and each Tesseract process directly, so timeout
cleanup kills and waits for the actual worker, including on Windows. Files live only in
a temporary directory, removed after success or failure.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

TOTAL_SECONDS = 60.0
RENDER_SECONDS = 10.0
PAGE_SECONDS = 15.0
DPI = 180
MAX_PIXELS = 8_000_000
MAX_EDGE = 4_000
MAX_PAGE_POINTS = 14_400
_SLOT = threading.BoundedSemaphore(1)
_TIMEOUT = "OCR took too long. Try fewer pages or a clearer scan, or fill in the profile form instead."


class OCRError(Exception):
    """A recoverable OCR failure, safe to show without internal process output."""


def _timeout(deadline: float, limit: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OCRError(_TIMEOUT)
    return min(limit, remaining)


def _run(command: list[str], *, deadline: float, limit: float, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
    environment = os.environ.copy()
    environment.pop("OPENROUTER_API_KEY", None)
    environment["OMP_THREAD_LIMIT"] = "1"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        # run() kills and waits on TimeoutExpired, and also on interrupted communicate().
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout,
                              stderr=stderr, env=environment,
                              creationflags=flags, timeout=_timeout(deadline, limit), check=False)
    except subprocess.TimeoutExpired as exc:
        raise OCRError(_TIMEOUT) from exc
    except OSError as exc:
        raise OCRError("OCR could not start. Check that Tesseract is installed and available on PATH.") from exc


def recognize_pages(data: bytes, pages: list[int], *, max_chars: int) -> dict[int, str]:
    """Read selected zero-based PDF pages. At most one OCR import runs at a time."""
    if not _SLOT.acquire(blocking=False):
        raise OCRError("Another resume is being read with OCR. Wait for it to finish, then try again.")
    try:
        engine = shutil.which("tesseract")
        if engine is None:
            raise OCRError("OCR needs Tesseract with English language data installed on this computer and "
                           "available on PATH. Install it, then try again; you can also fill in the profile form.")
        deadline = time.monotonic() + TOTAL_SECONDS
        with tempfile.TemporaryDirectory(prefix="resuapply-ocr-") as folder:
            folder = Path(folder)
            source = folder / "resume.pdf"
            source.write_bytes(data)
            languages = folder / "languages.txt"
            with languages.open("wb") as output:
                # Tesseract before 4.1 prints this list to stderr rather than stdout.
                checked = _run([engine, "--list-langs"], deadline=deadline, limit=5, stdout=output,
                               stderr=subprocess.STDOUT)
            with languages.open("rb") as output:
                available = output.read(64 * 1024).decode("utf-8", errors="replace").splitlines()
            if checked.returncode or "eng" not in [line.strip() for line in available]:
                raise OCRError("OCR needs Tesseract's English language data (eng). Install that language data, "
                               "then try again, or fill in the profile form.")
            result, remaining = {}, max_chars
            for page in pages:
                image = folder / "page.png"
                # -P: don't put this package's folder first on the child's sys.path, where its
                # modules (profile.py, text.py, ...) would shadow same-named library imports.
                rendered = _run([sys.executable, "-P", str(Path(__file__).resolve()), "--render",
                                 str(source), str(page), str(image)], deadline=deadline, limit=RENDER_SECONDS)
                if rendered.returncode == 73:
                    raise OCRError("A scanned page has invalid or unusually large dimensions. Save the resume "
                                   "again as a standard-sized PDF, then try importing it.")
                if rendered.returncode == 74:
                    raise OCRError("OCR's PDF renderer is unavailable. Reinstall this project's Python "
                                   "requirements, then try again.")
                if rendered.returncode or not image.is_file():
                    raise OCRError("A scanned PDF page could not be rendered. The PDF may be damaged; "
                                   "try saving it again as a PDF.")
                destination = folder / "recognized"
                recognized = _run([engine, str(image), str(destination), "-l", "eng", "--dpi", str(DPI),
                                   "--psm", "3"], deadline=deadline, limit=PAGE_SECONDS)
                text_file = destination.with_suffix(".txt")
                if recognized.returncode or not text_file.is_file():
                    raise OCRError("Tesseract could not read a scanned page. Try a clearer scan or fill in "
                                   "the profile form instead.")
                with text_file.open("rb") as output:
                    text = output.read(max(0, remaining) * 4).decode("utf-8", errors="replace")[:remaining]
                result[page] = text
                remaining -= len(text)
                image.unlink()
                text_file.unlink()
                if not remaining:
                    result.update((later, "") for later in pages if later not in result)
                    break
            return result
    except OSError as exc:
        raise OCRError("OCR could not use its temporary files. Check available disk space, then try again.") from exc
    finally:
        _SLOT.release()


def _render(source: str, page_index: int, destination: str) -> int:
    """Internal process entry point. No native PDFium operations occur in the web process."""
    try:
        import pypdfium2 as pdfium
        from PIL import Image  # noqa: F401  (check the image adapter before loading native PDFium)
    except ImportError:
        return 74
    try:
        with pdfium.PdfDocument(source, password="") as document:
            with closing(document[page_index]) as page:
                width, height = page.get_size()
                if not (math.isfinite(width) and math.isfinite(height) and
                        0 < width <= MAX_PAGE_POINTS and 0 < height <= MAX_PAGE_POINTS):
                    return 73
                scale = min(DPI / 72, math.sqrt((MAX_PIXELS - MAX_EDGE * 2) / (width * height)),
                            (MAX_EDGE - 1) / max(width, height))
                with closing(page.render(scale=scale, grayscale=True)) as bitmap:
                    if bitmap.width * bitmap.height > MAX_PIXELS or max(bitmap.width, bitmap.height) > MAX_EDGE:
                        return 73
                    with bitmap.to_pil() as image:
                        image.save(destination, format="PNG")
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--render":
        sys.exit(2)
    try:
        sys.exit(_render(sys.argv[2], int(sys.argv[3]), sys.argv[4]))
    except ValueError:
        sys.exit(2)
