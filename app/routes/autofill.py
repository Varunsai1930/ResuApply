"""Greenhouse autofill on the Submit and track step: attach the package's resume PDF, open, fill and close the form.

Every action is a form post the user makes; the browser never opens or fills on its own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from ..db import get_session
from ..services import autofill as autofill_service
from ..services import resume_attachment
from ..services.profile import get_candidate
from ..services.resume_import import MAX_BYTES
from .jobs import _job_or_404, _render_workspace

router = APIRouter(prefix="/jobs/{job_id}/autofill")

_UPLOAD_FRAMING = 64 * 1024  # room for the multipart framing around the file


def _done(job_id: int, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{job_id}?step=track&msg={msg}#autofill", status_code=303)


def _refused(request: Request, session: Session, job, message: str, status_code: int = 409):
    session.rollback()
    return _render_workspace(request, session, job, status_code=status_code, autofill_error=message)


@router.post("/resume")
async def attach_resume(request: Request, job_id: int, session: Session = Depends(get_session)):
    """Keep the PDF the user saved from the approved package. Read here, so an oversized upload is refused unread."""
    job = _job_or_404(session, job_id)
    length = request.headers.get("content-length", "")
    if length.isascii() and length.isdigit() and int(length) > MAX_BYTES + _UPLOAD_FRAMING:
        return _refused(request, session, job, f"The file is over {MAX_BYTES // (1024 * 1024)} MB. Attach the resume "
                        "PDF you saved from this package.", 413)
    async with request.form(max_files=1, max_fields=5) as form:
        file = form.get("file")
        file = file if isinstance(file, UploadFile) else None
        data = await file.read(MAX_BYTES + 1) if file is not None else b""
        filename = file.filename if file is not None else ""
        token = form.get("package_token")
        token = token if isinstance(token, str) else ""
    if not data:
        return _refused(request, session, job, "Choose the resume PDF you saved from this package.", 422)
    data_dir = request.app.state.settings.resolved_data_dir
    try:
        await run_in_threadpool(resume_attachment.attach, session, data_dir, job, get_candidate(session), data,
                                filename, token)
    except resume_attachment.AttachmentError as exc:
        return _refused(request, session, job, str(exc), 422)
    return _done(job.id, "resume_attached")


@router.post("/resume/remove")
def remove_resume(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    resume_attachment.remove(session, request.app.state.settings.resolved_data_dir, job, get_candidate(session))
    return _done(job.id, "resume_removed")


@router.post("/open")
def open_form(request: Request, job_id: int, package_token: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        request.app.state.autofill.open(job, get_candidate(session), package_token)
    except autofill_service.AutofillError as exc:
        return _refused(request, session, job, str(exc))
    return _done(job.id, "autofill_opened")


@router.post("/inspect")
def inspect_form(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        request.app.state.autofill.inspect(job, get_candidate(session))
    except autofill_service.AutofillError as exc:
        return _refused(request, session, job, str(exc))
    return _done(job.id, "autofill_opened")


@router.post("/fill")
def fill_form(request: Request, job_id: int, plan_token: str = Form(""), replace: list[str] = Form([]),
              session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        request.app.state.autofill.fill(job, get_candidate(session), plan_token, set(replace))
    except autofill_service.AutofillError as exc:
        return _refused(request, session, job, str(exc))
    return _done(job.id, "autofill_filled")


@router.post("/close")
def close_browser(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        request.app.state.autofill.close()
    except autofill_service.AutofillError as exc:
        return _refused(request, session, job, str(exc))
    return _done(job.id, "autofill_closed")
