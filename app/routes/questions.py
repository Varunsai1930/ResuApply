"""Application questions: add, answer, confirm, skip, categorize, AI drafts and the answer bank."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..ai import operations
from ..ai import answers as answers_ai
from ..ai.client import AIError, OpenRouterClient
from ..db import get_ai_client, get_session
from ..models import Job
from ..services import answers as answer_service
from ..services.answers import AnswerError, ReviewChanged
from ..services.profile import get_candidate
from .jobs import _job_or_404, _render_workspace

router = APIRouter(prefix="/jobs/{job_id}/questions")

AI_STATUS = {"busy": 409, "not_configured": 400}
NO_PROFILE = "Create your profile before answering questions. Answers are checked against it."


def _question_or_404(job: Job, qid: str) -> None:
    if not any(q.id == qid for q in job.questions):
        raise HTTPException(404, "Question not found.")


def _parse_limit(raw: str) -> int | None:
    """Empty means no limit. Anything that isn't a whole number becomes 0, which the service rejects in words."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return 0


def _back(job: Job, msg: str, qid: str | None = None) -> RedirectResponse:
    anchor = f"question-{qid}" if qid else "questions"
    return RedirectResponse(f"/jobs/{job.id}?msg={msg}#{anchor}", status_code=303)


def _failed(request: Request, session: Session, job: Job, message: str, status_code: int, qid: str | None = None,
            details: list[str] | None = None, text: str | None = None, form: dict | None = None):
    session.rollback()
    error = {"id": qid, "message": message, "details": details or [], "text": text}
    return _render_workspace(request, session, job, status_code=status_code, question_error=error, question_form=form)


def _act(request: Request, session: Session, job: Job, qid: str, action, msg: str, status_code: int = 422,
         text: str | None = None):
    """Run one service action for a question; a refusal re-renders the workspace at that question.

    A page that no longer matches what is stored (``ReviewChanged``) is a conflict: 409, with the
    current content shown for the user to review again.
    """
    _question_or_404(job, qid)
    try:
        action()
    except AnswerError as exc:
        code = 409 if isinstance(exc, ReviewChanged) else status_code
        return _failed(request, session, job, str(exc), code, qid, exc.details, text)
    return _back(job, msg, qid)


# ---------------------------------------------------------------- questions

@router.post("")
def add_question(request: Request, job_id: int, text: str = Form(""), required: str = Form(""),
                 limit: str = Form(""), limit_unit: str = Form("chars"), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    form = {"text": text, "required": bool(required), "limit": limit, "limit_unit": limit_unit}
    try:
        question = answer_service.add_question(session, job, text, bool(required), _parse_limit(limit), limit_unit)
    except AnswerError as exc:
        return _failed(request, session, job, str(exc), 422, details=exc.details, form=form)
    return _back(job, "question_added", question.id)


@router.post("/{qid}/remove")
def remove_question(request: Request, job_id: int, qid: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    _question_or_404(job, qid)
    try:
        answer_service.remove_question(session, job, job.application, qid)
    except AnswerError as exc:
        return _failed(request, session, job, str(exc), 422, qid, exc.details)
    return _back(job, "question_removed")


@router.post("/{qid}/category")
def set_category(request: Request, job_id: int, qid: str, category: str = Form(""),
                 session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _act(request, session, job, qid,
                lambda: answer_service.set_category(session, job, job.application, qid, category), "category_saved")


# ---------------------------------------------------------------- answers

@router.post("/{qid}/answer")
def save_answer(request: Request, job_id: int, qid: str, text: str = Form(""), origin: str = Form("user"),
                bank_id: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    raw_id = bank_id.strip()
    try:
        entry_id = int(raw_id) if raw_id.isascii() and raw_id.isdigit() else None
    except ValueError:
        entry_id = None
    return _act(request, session, job, qid,
                lambda: answer_service.set_answer(session, job, job.application, qid, text, origin, entry_id),
                "answer_saved", text=text)


@router.post("/{qid}/confirm")
def confirm_answer(request: Request, job_id: int, qid: str, confirmation_token: str = Form(""),
                   session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    _question_or_404(job, qid)
    candidate = get_candidate(session)
    if candidate is None:
        return _failed(request, session, job, NO_PROFILE, 400, qid)
    return _act(request, session, job, qid,
                lambda: answer_service.confirm_answer(session, job, job.application, qid, candidate,
                                                      confirmation_token),
                "answer_confirmed")


@router.post("/{qid}/skip")
def skip_answer(request: Request, job_id: int, qid: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _act(request, session, job, qid,
                lambda: answer_service.skip_answer(session, job, job.application, qid), "answer_skipped")


@router.post("/{qid}/bank")
def save_to_bank(request: Request, job_id: int, qid: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _act(request, session, job, qid,
                lambda: answer_service.save_to_bank(session, job, job.application, qid), "bank_saved")


# ---------------------------------------------------------------- AI drafts

@router.post("/draft")
def draft(request: Request, job_id: int, force: int = Form(0), session: Session = Depends(get_session),
          client: OpenRouterClient = Depends(get_ai_client)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    if candidate is None:
        return _failed(request, session, job, NO_PROFILE, 400)
    if not request.app.state.settings.ai_configured:
        return _failed(request, session, job, "AI is off. Write your answers yourself.", 400)
    try:
        answers_ai.draft_answers(session, client, request.app.state.settings, job, candidate, force=bool(force))
    except operations.ApprovalNeeded:
        return RedirectResponse(f"/profile/sharing?next={quote(f'/jobs/{job.id}')}", status_code=303)
    except operations.NothingToDo as exc:
        return _render_workspace(request, session, job, questions_notice=str(exc))
    except AIError as exc:
        return _failed(request, session, job, exc.message, AI_STATUS.get(exc.kind, 502))
    except AnswerError as exc:
        return _failed(request, session, job, str(exc), 409, details=exc.details)
    return _back(job, "drafts_ready")


@router.post("/{qid}/draft/accept")
def accept_draft(request: Request, job_id: int, qid: str, draft_token: str = Form(""),
                 session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    _question_or_404(job, qid)
    candidate = get_candidate(session)
    if candidate is None:
        return _failed(request, session, job, NO_PROFILE, 400, qid)
    return _act(request, session, job, qid,
                lambda: answer_service.accept_draft(session, job, job.application, qid, candidate, draft_token),
                "draft_accepted", status_code=409)


@router.post("/{qid}/draft/discard")
def discard_draft(request: Request, job_id: int, qid: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _act(request, session, job, qid,
                lambda: answer_service.discard_draft(session, job, job.application, qid), "draft_discarded",
                status_code=409)
