"""The resume PDF for Greenhouse autofill: the file the user saved from their approved package.

The app never makes this PDF itself; the browser's Save as PDF does, from the accepted resume.
So the user attaches it here, and it is checked before it is kept:

- it is a readable PDF of at most ``MAX_PAGES`` pages and ``MAX_BYTES``, without a password;
- its text contains the name the approved resume shows, so it is a resume of this candidate;
- it is attached to the package the page showed (``package_token``), and only while that package
  is Approved. The record keeps the approved package's hash: once the package changes, the PDF
  no longer counts as its resume and autofill asks for it again.

The file lives in the data folder (``attachments/job-{id}.pdf``, git-ignored with the rest of
``data/``). Its SHA-256 is checked again before the browser receives it.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
from pathlib import Path

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application, Candidate, Job
from ..schemas.autofill import ResumeAttachment
from ..schemas.tracking import ReviewState
from . import package, transactions
from .resume_import import MAX_BYTES

MAX_PAGES = 10  # a resume longer than this is not the one-to-few-page PDF the package prints
FOLDER = "attachments"
DEFAULT_NAME = "Resume.pdf"
_UNSAFE = re.compile(r"[^\w .()-]+")


class AttachmentError(Exception):
    """A user-facing error. Nothing was changed."""


def path_for(data_dir: Path, job_id: int) -> Path:
    return data_dir / FOLDER / f"job-{job_id}.pdf"


def safe_name(filename: str | None) -> str:
    """The file name the employer's form receives: the user's own, without folders or unusual characters."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = re.sub(r"\s+", " ", _UNSAFE.sub("-", stem)).strip(" .-")[:100]
    return f"{stem}.pdf" if stem else DEFAULT_NAME


def _squash(text: str) -> str:
    """Case, accents' composed form and whitespace aside: PDF text often splits or joins words."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def read_pdf(data: bytes) -> tuple[int, str]:
    """The page count and text of a resume PDF, or AttachmentError with what to do instead."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    if len(data) > MAX_BYTES:
        raise AttachmentError(f"The file is over {MAX_BYTES // (1024 * 1024)} MB. Attach the resume PDF you "
                              "saved from this package.")
    if not data.startswith(b"%PDF-"):
        raise AttachmentError("That file isn't a PDF. Open the accepted resume, choose Print / save PDF, save it "
                              "as a PDF and attach that file.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise AttachmentError("This PDF is password-protected. Employers' forms can't read it; save the "
                                  "resume again without a password.")
        pages = len(reader.pages)
        if not 1 <= pages <= MAX_PAGES:
            raise AttachmentError(f"This PDF has {pages} pages; a resume saved from the package has at most "
                                  f"{MAX_PAGES}. Attach the resume PDF you saved from this package.")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except AttachmentError:
        raise
    except (PyPdfError, ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OSError):
        raise AttachmentError("This PDF couldn't be read. It may be damaged; save the resume as a PDF again and "
                              "attach that file.") from None
    return pages, text


def resume_name(job: Job, application: Application, candidate: Candidate | None) -> str:
    resume = package.resolved_package(job, application, candidate)["resume"] or {}
    return ((resume.get("contact") or {}).get("name") or "").strip()


def attach(session: Session, data_dir: Path, job: Job, candidate: Candidate | None, data: bytes,
           filename: str | None, token: str) -> ResumeAttachment:
    """Keep the PDF as the resume of the approved package the page showed. Replaces an earlier one."""
    with transactions.write(session, job, candidate):
        application = job.application
        if package.review_state(job, application, candidate) is not ReviewState.APPROVED:
            raise AttachmentError("Approve the current package first. The PDF belongs to an approved package, "
                                  "so save it from the accepted resume after approving.")
        if token != package.package_token(job, application, candidate):
            raise AttachmentError("The package changed since you reviewed it, so the PDF wasn't attached. Review "
                                  "the package as it is now, then attach the PDF saved from it.")
        pages, text = read_pdf(data)
        name = resume_name(job, application, candidate)
        if not name or _squash(name) not in _squash(text):
            raise AttachmentError("This PDF doesn't show your name as the approved resume does, so it may not be "
                                  "the package's resume. Open the accepted resume, choose Print / save PDF and "
                                  "attach that file.")
        target = path_for(data_dir, job.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        try:
            partial.write_bytes(data)
            os.replace(partial, target)
        except OSError:
            partial.unlink(missing_ok=True)
            raise AttachmentError("The PDF couldn't be saved in the data folder. Check the free disk space, "
                                  "then try again.") from None
        attachment = ResumeAttachment(
            filename=safe_name(filename), sha256=hashlib.sha256(data).hexdigest(), size=len(data), pages=pages,
            package_hash=application.approval.content_hash, attached_at=utcnow(),
        )
        application.resume_attachment = attachment
    return attachment


def current(job: Job, application: Application, candidate: Candidate | None) -> ResumeAttachment | None:
    """The attached PDF while it belongs to the package as it is now: Approved, with the hash it was attached to."""
    attachment = application.resume_attachment
    if attachment is None or application.approval is None:
        return None
    if package.review_state(job, application, candidate) is not ReviewState.APPROVED:
        return None
    return attachment if attachment.package_hash == application.approval.content_hash else None


def load(data_dir: Path, job: Job, attachment: ResumeAttachment) -> bytes:
    """The attached file's bytes, refused when it is missing or no longer the file that was checked."""
    try:
        data = path_for(data_dir, job.id).read_bytes()
    except OSError:
        data = b""
    if hashlib.sha256(data).hexdigest() != attachment.sha256:
        raise AttachmentError("The attached PDF is missing from the data folder or was changed there. Attach it "
                              "again.")
    return data


def remove(session: Session, data_dir: Path, job: Job, candidate: Candidate | None) -> None:
    with transactions.write(session, job, candidate):
        job.application.resume_attachment = None
    remove_files(data_dir, [job.id])


def remove_files(data_dir: Path, job_ids: list[int]) -> None:
    """Delete the attached PDFs of these jobs, if any (a removed job or attachment)."""
    for job_id in job_ids:
        try:
            path_for(data_dir, job_id).unlink(missing_ok=True)
        except OSError:
            pass
