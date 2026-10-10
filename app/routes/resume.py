"""Review, explicitly accept and print a resume derived from the saved profile."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..ai import operations, prompts
from ..ai.client import AIError, OpenRouterClient
from ..ai.resume import tailor_resume
from ..db import get_ai_client, get_session
from ..models import Application
from ..services import outbound, resume as resume_service
from ..services.profile import get_candidate, skill_keys
from ..templating import templates
from .jobs import _job_or_404, _render_workspace

router = APIRouter(prefix="/jobs/{job_id}/resume")


def presentation_state(session, settings, job, candidate):
    """One freshness rule for the workspace, review and acceptance screens."""
    current = resume_service.state(job, candidate)
    proposal = current.proposal
    if proposal is not None and proposal.model and candidate is not None:
        sharing = outbound.state(session, settings, candidate)
        stale = (
            proposal.model != settings.openrouter_model
            or proposal.prompt_revision != prompts.TAILOR_RESUME_REVISION
            or not sharing.ready
            or proposal.outbound_hash != sharing.context_hash
        )
        if stale:
            current = replace(current, proposal_stale=True)
    return current


def _failure(request, session, job, message, status_code=409):
    session.rollback()
    return _render_workspace(request, session, job, status_code=status_code, resume_error=message)


def _lock_acceptance(session, job, candidate):
    """Check sharing and proposal freshness under SQLite's database writer lock."""
    # Preserve the actual timestamp even if another request changed it before us.
    session.execute(update(Application).where(Application.id == job.application.id)
                    .values(updated_at=Application.updated_at).execution_options(synchronize_session=False))
    session.refresh(job)
    session.refresh(job.application)
    if candidate is not None:
        session.refresh(candidate)
        approval = outbound.get_approval(session, candidate)
        if approval is not None:
            session.refresh(approval)


@router.post("/profile")
def propose_profile(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    if candidate is None:
        return _failure(request, session, job, "Create your profile before preparing a resume.", 400)
    try:
        resume_service.propose(session, job, candidate, resume_service.profile_draft(candidate.profile))
    except resume_service.ResumeError as exc:
        return _failure(request, session, job, str(exc), 422)
    return RedirectResponse(f"/jobs/{job.id}/resume/review", status_code=303)


@router.post("/generate")
def generate(request: Request, job_id: int, force: int = Form(0), session: Session = Depends(get_session),
             client: OpenRouterClient = Depends(get_ai_client)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    if candidate is None:
        return _failure(request, session, job, "Create your profile before tailoring a resume.", 400)
    if not request.app.state.settings.ai_configured:
        return _failure(request, session, job, "AI is off. Use your profile as-is to prepare a resume.", 400)
    try:
        tailor_resume(session, client, request.app.state.settings, job, candidate, force=bool(force))
    except operations.ApprovalNeeded:
        return RedirectResponse(f"/profile/sharing?next={quote(f'/jobs/{job.id}?step=resume')}", status_code=303)
    except AIError as exc:
        return _failure(request, session, job, exc.message, {"busy": 409, "not_configured": 400}.get(exc.kind, 502))
    except resume_service.ResumeError as exc:
        return _failure(request, session, job, str(exc), 409)
    return RedirectResponse(f"/jobs/{job.id}/resume/review", status_code=303)


def _selection(profile, content):
    result = []
    for name, label in (("experience", "Experience"), ("projects", "Projects"),
                        ("education", "Education"), ("certifications", "Certifications"), ("skills", "Skills")):
        available = len(skill_keys(profile)) if name == "skills" else len(getattr(profile, name))
        result.append({"label": label, "selected": len(getattr(content, name)), "available": available})
    return result


@router.get("/review", response_class=HTMLResponse)
def review(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    current = presentation_state(session, request.app.state.settings, job, candidate)
    if current.proposal is None:
        return _failure(request, session, job, "Prepare a resume proposal before opening its review.", 404)
    record = current.proposal
    rendered = None
    if not current.proposal_stale and candidate is not None:
        try:
            rendered = resume_service.render_context(candidate.profile, record.resume)
        except resume_service.ResumeError as exc:
            return _failure(request, session, job, str(exc), 409)
    return templates.TemplateResponse(request, "resume_review.html", {
        "job": job, "candidate": candidate, "record": record, "resume_state": current,
        "rendered": rendered, "selection": _selection(candidate.profile, record.resume) if rendered else [],
        "proposal_token": resume_service.proposal_token(record),
        "sharing": outbound.state(session, request.app.state.settings, candidate) if candidate else None,
        "settings": request.app.state.settings, "active": "jobs",
    })


@router.post("/accept")
def accept(request: Request, job_id: int, proposal_token: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    _lock_acceptance(session, job, candidate)
    current = presentation_state(session, request.app.state.settings, job, candidate)
    if candidate is None or current.proposal is None:
        return _failure(request, session, job, "Prepare and review a resume proposal before accepting it.", 400)
    if current.proposal_stale:
        return _failure(request, session, job,
                        "This resume proposal is out of date. Prepare a fresh proposal, review it and accept it.")
    try:
        resume_service.accept(session, job, candidate, proposal_token=proposal_token)
    except resume_service.ResumeError as exc:
        return _failure(request, session, job, str(exc))
    return RedirectResponse(f"/jobs/{job.id}?step=resume&msg=resume_accepted#resume", status_code=303)


@router.get("/print", response_class=HTMLResponse)
def print_resume(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    current = presentation_state(session, request.app.state.settings, job, candidate)
    if current.accepted is None:
        return _failure(request, session, job, "Review and accept a resume before printing it.", 400)
    if current.accepted_stale or candidate is None:
        return _failure(request, session, job,
                        "Your accepted resume is out of date because the profile or job changed. "
                        "Prepare a fresh proposal, review it and accept it before printing.")
    try:
        rendered = resume_service.render_context(candidate.profile, current.accepted.resume)
    except resume_service.ResumeError as exc:
        return _failure(request, session, job, str(exc))
    return templates.TemplateResponse(request, "resume_print.html", {
        "job": job, "record": current.accepted, "rendered": rendered,
    })
