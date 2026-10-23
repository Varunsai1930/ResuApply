"""Review, explicitly accept and print a resume derived from the saved profile."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..ai import operations, prompts, tasks
from ..ai.client import AIError, OpenRouterClient
from ..ai.resume import TAILOR_RESUME, tailor_resume
from ..db import get_ai_client, get_session
from ..models import Application
from ..services import outbound, package, resume_docx, resume_layouts, resume_pdf, resume as resume_service
from ..services.profile import get_candidate, skill_keys
from ..services.resume_quality import quality_notes
from ..templating import templates
from .jobs import _job_or_404, _render_workspace
from .resume_download import docx_response, pdf_response, preview_image, preview_response

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
    if get_candidate(session) is None:
        return _failure(request, session, job, "Create your profile before tailoring a resume.", 400)
    settings = request.app.state.settings
    if not settings.ai_configured:
        return _failure(request, session, job, "AI is off. Use your profile as-is to prepare a resume.", 400)

    work = tasks.with_profile(lambda worker_session, worker_job, candidate: tailor_resume(
        worker_session, client, settings, worker_job, candidate, force=bool(force)))
    task = tasks.start(request, TAILOR_RESUME, job, work, _generated)
    return tasks.respond(request, session, job, task)


def _generated(request: Request, session: Session, job, task: tasks.Task):
    try:
        task.result()
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
        "quality_notes": quality_notes(candidate.profile, record.resume) if rendered else [],
        "proposal_token": resume_service.proposal_token(record),
        "selected_layout": resume_layouts.layout_key(job.application),
        "layout_choices": resume_layouts.LAYOUTS,
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


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, job_id: int, version: str = "", session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    versions = list(reversed(job.application.resume_versions))
    chosen = next((v for v in versions if v.id == version), None) if version else next(iter(versions), None)
    if version and chosen is None:
        raise HTTPException(404, "Resume version not found.")
    candidate = get_candidate(session)
    current = resume_service.state(job, candidate)
    return templates.TemplateResponse(request, "resume_history.html", {
        "job": job, "versions": versions, "chosen": chosen,
        "previous": resume_service.render_context(chosen.profile, chosen.record.resume) if chosen else None,
        "current": resume_service.render_context(candidate.profile, current.accepted.resume)
                   if candidate and current.accepted and not current.accepted_stale else None,
        "active": "jobs",
    })


@router.post("/history/{version}/restore")
def restore_version(request: Request, job_id: int, version: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    chosen = next((v for v in job.application.resume_versions if v.id == version), None)
    if chosen is None:
        raise HTTPException(404, "Resume version not found.")
    candidate = get_candidate(session)
    if candidate is None:
        return _failure(request, session, job, "Create your profile before restoring a version.", 400)
    try:
        resume_service.propose(session, job, candidate, chosen.record.resume)
    except resume_service.ResumeError as exc:
        return _failure(request, session, job, "This version no longer matches your profile. " + str(exc))
    return RedirectResponse(f"/jobs/{job.id}/resume/review", status_code=303)


@router.get("/print", response_class=HTMLResponse)
def print_resume(request: Request, job_id: int, msg: str = "", preview_layout: str | None = None,
                 session: Session = Depends(get_session)):
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
        if preview_layout is not None:
            resume_layouts.get_layout(preview_layout)
        rendered = resume_service.render_context(candidate.profile, current.accepted.resume)
    except (resume_service.ResumeError, ValueError) as exc:
        return _failure(request, session, job, str(exc))
    return templates.TemplateResponse(request, "resume_print.html", {
        "job": job, "record": current.accepted, "rendered": rendered, "msg": msg,
        "selected_layout": resume_layouts.layout_key(job.application),
        "layout_choices": resume_layouts.LAYOUTS,
        "layout_token": package.package_token(job, job.application, candidate),
    })


@router.post("/layout")
def choose_layout(request: Request, job_id: int, layout: str = Form(""), package_token: str = Form(""),
                  session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        resume_layouts.get_layout(layout)
    except ValueError:
        return _failure(request, session, job, "Choose one of the available resume layouts.", 422)
    try:
        changed = package.set_resume_layout(session, job, get_candidate(session), layout, package_token)
    except package.PackageError as exc:
        return _failure(request, session, job, str(exc))
    return RedirectResponse(f"/jobs/{job.id}/resume/print?msg={'layout_changed' if changed else 'layout_unchanged'}",
                            status_code=303)


@router.get("/docx")
def download_docx(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    current = presentation_state(session, request.app.state.settings, job, candidate)
    if current.accepted is None:
        return _failure(request, session, job, "Review and accept a resume before downloading it.", 400)
    if current.accepted_stale or candidate is None:
        return _failure(request, session, job,
                        "Your accepted resume is out of date. Prepare a fresh proposal, review it and accept it "
                        "before downloading.")
    try:
        rendered = resume_service.render_context(candidate.profile, current.accepted.resume)
        content = resume_docx.export_resume(rendered, layout=resume_layouts.layout_key(job.application))
    except (resume_service.ResumeError, resume_docx.ResumeDocxError) as exc:
        return _failure(request, session, job, str(exc), 422)
    return docx_response(content, rendered["contact"]["name"], job.company)


def _pdf_document(request, session, job_id, layout=None, background=False):
    """Preview choices are read-only. Downloads always use the persisted layout."""
    job = _job_or_404(session, job_id)
    candidate = get_candidate(session)
    current = presentation_state(session, request.app.state.settings, job, candidate)
    if current.accepted is None:
        raise HTTPException(400, "Review and accept a resume before downloading or previewing it.")
    if current.accepted_stale or candidate is None:
        raise HTTPException(409, "Your accepted resume is out of date. Review and accept a fresh resume first.")
    try:
        selected = resume_layouts.get_layout(layout).key if layout is not None else resume_layouts.layout_key(job.application)
        rendered = resume_service.render_context(candidate.profile, current.accepted.resume)
        browser = request.app.state.settings.autofill_browser
        document = resume_pdf.export_resume(rendered, selected, str(browser.expanduser()) if browser else None,
                                            queue=request.app.state.pdf_previews if background else None)
    except resume_pdf.PDFError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, resume_service.ResumeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return job, rendered, document, selected


@router.get("/pdf")
def download_pdf(request: Request, job_id: int, session: Session = Depends(get_session)):
    job, rendered, document, _ = _pdf_document(request, session, job_id)
    return pdf_response(document.content, rendered["contact"]["name"], job.company)


@router.get("/preview")
def preview_pdf(request: Request, job_id: int, layout: str | None = None, background: bool = False,
                session: Session = Depends(get_session)):
    try:
        _, _, document, selected = _pdf_document(request, session, job_id, layout, background)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})
    return preview_response(document, f"/jobs/{job_id}/resume/preview", selected)


@router.get("/preview/{page_number}.png")
def preview_pdf_page(request: Request, job_id: int, page_number: int, version: str = "", layout: str | None = None,
                     session: Session = Depends(get_session)):
    _, _, document, _ = _pdf_document(request, session, job_id, layout)
    return preview_image(document, page_number, version)
