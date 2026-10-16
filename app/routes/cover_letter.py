"""The cover letter: AI draft, accept or discard, write your own, remove, print and Word export."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..ai import cover_letter as letter_ai
from ..ai import operations, tasks
from ..ai.client import AIError, OpenRouterClient
from ..db import get_ai_client, get_session
from ..models import Job
from ..services import cover_letter as letter_service
from ..services import resume_docx, resume_layouts
from ..services.profile import get_candidate
from ..templating import templates
from .jobs import _job_or_404, _render_workspace
from .resume_download import docx_response

router = APIRouter(prefix="/jobs/{job_id}/cover-letter")

AI_STATUS = {"busy": 409, "not_configured": 400}
NO_PROFILE = "Create your profile before preparing a cover letter. Its facts come from your profile."


def _back(job: Job, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{job.id}?step=questions&msg={msg}#cover-letter", status_code=303)


def _failed(request: Request, session: Session, job: Job, message: str, status_code: int,
            details: list[str] | None = None, text: str | None = None):
    session.rollback()
    return _render_workspace(request, session, job, status_code=status_code,
                             cover_letter_error={"message": message, "details": details or [], "text": text})


def _act(request: Request, session: Session, job: Job, action, msg: str, status_code: int = 422,
         text: str | None = None):
    """Run one service action; a refusal re-renders the workspace at the letter (409 when the page is out of date)."""
    try:
        action()
    except letter_service.CoverLetterError as exc:
        code = 409 if isinstance(exc, letter_service.ReviewChanged) else status_code
        return _failed(request, session, job, str(exc), code, exc.details, text)
    return _back(job, msg)


@router.post("/draft")
def draft(request: Request, job_id: int, force: int = Form(0), session: Session = Depends(get_session),
          client: OpenRouterClient = Depends(get_ai_client)):
    job = _job_or_404(session, job_id)
    if get_candidate(session) is None:
        return _failed(request, session, job, NO_PROFILE, 400)
    settings = request.app.state.settings
    if not settings.ai_configured:
        return _failed(request, session, job, "AI is off. Write the cover letter yourself.", 400)

    work = tasks.with_profile(lambda worker_session, worker_job, candidate: letter_ai.draft_cover_letter(
        worker_session, client, settings, worker_job, candidate, force=bool(force)))
    task = tasks.start(request, letter_ai.DRAFT_COVER_LETTER, job, work, _drafted)
    return tasks.respond(request, session, job, task)


def _drafted(request: Request, session: Session, job: Job, task: tasks.Task):
    try:
        task.result()
    except operations.ApprovalNeeded:
        return RedirectResponse(f"/profile/sharing?next={quote(f'/jobs/{job.id}?step=questions#cover-letter')}",
                                status_code=303)
    except AIError as exc:
        return _failed(request, session, job, exc.message, AI_STATUS.get(exc.kind, 502), exc.problems)
    except letter_service.CoverLetterError as exc:
        return _failed(request, session, job, str(exc), 409, exc.details)
    return _back(job, "cover_letter_drafted")


@router.post("/draft/accept")
def accept_draft(request: Request, job_id: int, draft_token: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    if candidate is None:
        return _failed(request, session, job, NO_PROFILE, 400)
    return _act(request, session, job, lambda: letter_service.accept_draft(session, job, candidate, draft_token),
                "cover_letter_accepted", status_code=409)


@router.post("/draft/discard")
def discard_draft(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _act(request, session, job, lambda: letter_service.discard_draft(session, job),
                "cover_letter_draft_discarded", status_code=409)


@router.post("")
def save(request: Request, job_id: int, text: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    if get_candidate(session) is None:
        return _failed(request, session, job, NO_PROFILE, 400, text=text)
    return _act(request, session, job, lambda: letter_service.save_letter(session, job, text),
                "cover_letter_saved", text=text)


@router.post("/remove")
def remove(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _act(request, session, job, lambda: letter_service.remove_letter(session, job),
                "cover_letter_removed", status_code=409)


def _current_document(request: Request, session: Session, job: Job):
    """The accepted letter as it prints now, or a workspace error response."""
    candidate = get_candidate(session)
    letter = job.application.cover_letter
    if candidate is None:
        return None, _failed(request, session, job, NO_PROFILE, 400)
    if letter is None:
        return None, _failed(request, session, job, "Accept a draft or write a cover letter first.", 400)
    contact = candidate.profile.model_dump()["contact"]
    return letter_service.document(contact, job.company, letter.text), None


@router.get("/print")
def print_letter(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    letter, failure = _current_document(request, session, job)
    if failure is not None:
        return failure
    return templates.TemplateResponse(request, "cover_letter_print.html", {
        "job": job, "letter": letter, "selected_layout": resume_layouts.layout_key(job.application),
        "back_href": f"/jobs/{job.id}?step=questions#cover-letter", "back_label": "← Job workspace",
        "heading": "Cover letter",
        "note": "Choose Print, then Save as PDF. Use A4 paper and disable the browser's headers and footers. "
                "It uses the layout chosen for this job's resume.",
        "docx_href": f"/jobs/{job.id}/cover-letter/docx",
    })


@router.get("/docx")
def download_docx(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    letter, failure = _current_document(request, session, job)
    if failure is not None:
        return failure
    try:
        content = resume_docx.export_cover_letter(letter, layout=resume_layouts.layout_key(job.application))
    except resume_docx.ResumeDocxError as exc:
        return _failed(request, session, job, str(exc), 422)
    return docx_response(content, letter["name"], job.company, kind="cover letter")
