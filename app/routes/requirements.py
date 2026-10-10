"""Requirements editor, AI extraction, evidence links, suggestions and overrides for one job."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..ai import operations
from ..ai.client import AIError, OpenRouterClient
from ..db import get_ai_client, get_session
from ..models import Job
from ..services import checklist
from ..services.profile import FieldError, get_candidate
from ..services.requirements import RequirementsConflict, RequirementsInvalid, set_requirements
from ..services.requirements_form import CRITERION_LABELS, parse_form, to_rows
from ..schemas.requirements import CATEGORIES, IMPORTANCE
from ..templating import templates
from .jobs import _job_or_404, _render_workspace, reviewed_revision_of

router = APIRouter(prefix="/jobs/{job_id}")

AI_STATUS = {"busy": 409, "not_configured": 400}


def _back(job: Job, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/jobs/{job.id}?msg={msg}#requirements", status_code=303)


def _render_editor(request: Request, job: Job, items, errors=(), status_code: int = 200, notice: str = "",
                   base_revision: object = None, conflict: bool = False):
    """``base_revision`` is the saved revision the shown input is based on (default: the current one).

    On a conflict it stays the older revision the user started from, and the page shows the saved
    requirements with an explicit choice to save over them.
    """
    errors = list(errors)
    rows = to_rows(items) or to_rows([{}])
    return templates.TemplateResponse(
        request,
        "requirements_form.html",
        {
            "job": job,
            "base_revision": job.revision if base_revision is None else base_revision,
            "conflict": conflict,
            "rows": rows,
            "errors": errors,
            "error_fields": {e.field for e in errors if e.field},
            "notice": notice,
            "categories": CATEGORIES,
            "importance": IMPORTANCE,
            "criterion_labels": CRITERION_LABELS,
            "active": "jobs",
        },
        status_code=status_code,
    )


def _ai_failed(request: Request, session: Session, job: Job, exc: AIError):
    return _render_workspace(request, session, job, status_code=AI_STATUS.get(exc.kind, 502), ai_error=exc.message)


# ---------------------------------------------------------------- editor

@router.get("/requirements/edit", response_class=HTMLResponse)
def edit_requirements(request: Request, job_id: int, proposal: int = 0, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    if proposal:
        run = operations.cached_proposal(session, job, request.app.state.settings.openrouter_model)
        if run is not None:
            notice = ("These requirements were proposed by the AI model. Check each one against the description, "
                      "correct anything wrong, then save.")
            if job.requirements:
                notice += " Saving replaces the current list; requirements that stay the same keep their evidence."
            return _render_editor(request, job, run.result["requirements"], notice=notice)
    return _render_editor(request, job, job.requirements)


@router.post("/requirements")
async def save_requirements(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    form = await request.form()
    items = parse_form((k, v) for k, v in form.multi_items() if isinstance(v, str))
    raw_revision = form.get("base_revision")
    reviewed_revision = reviewed_revision_of(raw_revision, form.get("replace_revision"))
    try:
        if reviewed_revision is None:
            raise RequirementsConflict("The editor's saved revision is missing. Your input is kept below. "
                                       "Compare it with the saved requirements before saving again.")
        changed = set_requirements(session, job, items, base_revision=reviewed_revision)
    except RequirementsConflict as exc:
        return _render_editor(request, job, items, [FieldError(str(exc), "replace_revision")], status_code=409,
                              base_revision=raw_revision if isinstance(raw_revision, str) else "", conflict=True)
    except RequirementsInvalid as exc:
        return _render_editor(request, job, items, exc.errors, status_code=422, base_revision=reviewed_revision)
    return _back(job, "requirements_saved" if changed else "requirements_unchanged")


# ---------------------------------------------------------------- AI

@router.post("/requirements/extract")
def extract(request: Request, job_id: int, force: int = Form(0), session: Session = Depends(get_session),
            client: OpenRouterClient = Depends(get_ai_client)):
    job = _job_or_404(session, job_id)
    try:
        outcome = operations.extract_requirements(session, client, job, force=bool(force))
    except AIError as exc:
        return _ai_failed(request, session, job, exc)
    if outcome.problems:
        notice = ("The AI model's proposal still has problems after one correction attempt, so nothing was saved. "
                  "Fix the marked requirements (or remove them), then save.")
        return _render_editor(request, job, outcome.items, outcome.problems, status_code=422, notice=notice)
    return RedirectResponse(f"/jobs/{job.id}/requirements/edit?proposal=1", status_code=303)


@router.post("/evidence/suggest")
def suggest(request: Request, job_id: int, force: int = Form(0), session: Session = Depends(get_session),
            client: OpenRouterClient = Depends(get_ai_client)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    if candidate is None:
        return _render_workspace(request, session, job, status_code=400, ai_error="Create your profile first.")
    try:
        run = operations.suggest_evidence(session, client, request.app.state.settings, job, candidate, force=bool(force))
    except operations.ApprovalNeeded:
        return RedirectResponse(f"/profile/sharing?next={quote(f'/jobs/{job.id}')}", status_code=303)
    except operations.NothingToDo as exc:
        return _render_workspace(request, session, job, ai_notice=str(exc))
    except AIError as exc:
        return _ai_failed(request, session, job, exc)
    return _back(job, "suggestions_ready" if run.result["suggestions"] else "suggestions_none")


# ---------------------------------------------------------------- evidence and overrides

def _checklist_action(request: Request, session: Session, job: Job, req_id: str, action, msg: str):
    try:
        action()
    except checklist.ChecklistError as exc:
        status = 409 if isinstance(exc, checklist.EvidenceReviewChanged) else 422
        # A removed requirement has no card on which to display an inline error.
        removed = not any(req.id == req_id for req in job.requirements)
        return _render_workspace(request, session, job, status_code=status,
                                 check_error={"id": req_id, "message": str(exc)},
                                 ai_error=str(exc) if removed else None)
    return _back(job, msg)


@router.post("/requirements/{req_id}/evidence")
async def link_evidence(request: Request, job_id: int, req_id: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    form = await request.form()
    sources = [v for v in form.getlist("sources") if isinstance(v, str)]
    reviewed_token = form.get("review_token", "")
    if not isinstance(reviewed_token, str):
        reviewed_token = ""
    candidate = get_candidate(session)
    return _checklist_action(request, session, job, req_id,
                             lambda: checklist.link(session, job, candidate, req_id, sources, reviewed_token), "evidence_linked")


@router.post("/requirements/{req_id}/evidence/remove")
def unlink_evidence(request: Request, job_id: int, req_id: str, source: str = Form(""),
                    session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _checklist_action(request, session, job, req_id,
                             lambda: checklist.unlink(session, job, req_id, source), "evidence_removed")


@router.post("/requirements/{req_id}/suggestions/accept")
def accept_suggestion(request: Request, job_id: int, req_id: str, source: str = Form(""),
                      review_token: str = Form(""),
                      session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    return _checklist_action(request, session, job, req_id,
                             lambda: checklist.link(session, job, candidate, req_id, [source], review_token), "evidence_linked")


@router.post("/requirements/{req_id}/suggestions/reject")
def reject_suggestion(request: Request, job_id: int, req_id: str, source: str = Form(""),
                      session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return _checklist_action(request, session, job, req_id,
                             lambda: checklist.reject_suggestion(session, job, req_id, source), "suggestion_rejected")


@router.post("/requirements/{req_id}/override")
def override(request: Request, job_id: int, req_id: str, status: str = Form(""), reason: str = Form(""),
             session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    msg = "override_cleared" if status == "clear" else "override_saved"
    return _checklist_action(request, session, job, req_id,
                             lambda: checklist.set_override(session, job, req_id, status, reason), msg)
